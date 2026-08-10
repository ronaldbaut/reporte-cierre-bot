import discord
from discord.ext import commands
import os
import traceback
from openai import OpenAI
import json
import re
 
print(">>> Iniciando reportes-de-batidoras-bot...")
 
# ================== CONFIGURACIÓN ==================
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
if not DISCORD_TOKEN:
    raise RuntimeError("❌ ERROR: Falta la variable de entorno DISCORD_TOKEN")
 
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
 
bot = commands.Bot(command_prefix="!", intents=intents)
 
client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
)
 
# Estado por canal:
# {
#   "tipo": "encendido_batidoras" | "funcionamiento_batidoras" | "apagado_batidoras",
#   "user_id": int,
#   "batidora": 1..5,
#   "fase": "hora" | "checklist" | "video" | "funcionamiento",
#   "buffer": str,
#   "answers": {...},
#   "videos_usados": [filename o url, ...],  # evita reutilizar el mismo video en 2 batidoras
#   "hora_global": str | None,
# }
conversation_state = {}
 
NUM_BATIDORAS = 5
 
# ================== TEXTOS ==================
TXT_CHECKLIST_ENCENDIDO = (
    "Revisa lo siguiente **antes de encender**: "
    "(tensión de correas, chavetas del eje de batido, engrase del piñón, "
    "protección del rodamiento 6206, nivel óptimo de agua sal, ajuste del piñón sin juego, "
    "dientes del piñón —si faltan, cuántos—, movimiento del tambor derecha-izquierda y arriba-abajo).\n\n"
    "Reporta el estado. Si la batidora **no funciona / no se enciende / está en reparación**, dilo claro."
)
 
TXT_VIDEO = (
    "Por favor envía un **video del piñón de la batidora {bat}** "
    "(muestra los dientes y el estado general). "
    "No reutilices el video de otra batidora."
)
 
TXT_CHECKLIST_APAGADO = (
    "Al apagar reporta:\n"
    "• Dientes del piñón (completos o cuántos faltan — no digas solo “igual”)\n"
    "• Ajuste del piñón / grasa\n"
    "• Cuchilla y protección del rodamiento (arriba/abajo)\n"
    "• Agua sal\n"
    "• Hora de encendido y de apagado (si se apagó y se prendió de nuevo, pon los tramos)\n"
    "• Tiempos aproximados de batida (normal/gourmet/clásica/batipop/sundae según lo que usaron)\n"
    "Si **no funcionó hoy**, dilo y pasamos a la siguiente."
)
 
TXT_FUNCIONAMIENTO = (
    "Verifica durante el funcionamiento:\n"
    "• Temperatura del cabezote (ideal bajo 50°) y temperatura actual\n"
    "• Si está raspando bien la mezcla\n"
    "Si la batidora no está en uso / no funciona, dilo claro."
)
 
 
# ================== HELPERS ==================
def _vacio_o_basura(texto: str, tiene_media: bool) -> bool:
    t = (texto or "").strip()
    if tiene_media:
        return False
    if not t:
        return True
    return t.lower() in {".", "..", "...", "ok", "vale", "listo", "k", "ya", "-", "—"}
 
 
def _parece_cancelar(content_lower: str) -> bool:
    patrones = [
        r"^cancelar(\s+reporte)?$",
        r"^cancel(\s+report)?$",
        r"^cancela(r)?$",
        r"^canelar(\s+reporte)?$",
        r"^reporte\s+cancelado$",
        r"^detener(\s+reporte)?$",
    ]
    return any(re.match(p, content_lower) for p in patrones)
 
 
def _tiene_media(message: discord.Message) -> bool:
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        name = (att.filename or "").lower()
        if ct.startswith("video/") or ct.startswith("image/"):
            return True
        if name.endswith((".mp4", ".mov", ".webm", ".mkv", ".avi", ".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return True
    return False
 
 
def _media_ids(message: discord.Message) -> list:
    """Identificadores estables del adjunto para detectar reuso."""
    ids = []
    for att in message.attachments:
        # filename + size es un proxy razonable; url cambia por expiry
        ids.append(f"{att.filename}|{att.size}")
    return ids
 
 
def _msg_inicio(tipo: str, user: discord.abc.User) -> str:
    base_cancel = (
        f"_Solo {user.display_name} puede responder este reporte._\n"
        f"_Escribe **cancelar** o **cancelar reporte** para detenerlo._"
    )
    if tipo == "encendido_batidoras":
        return (
            f"**Reporte de Encendido de Batidoras iniciado** por {user.mention}\n\n"
            f"¿A qué hora se encendieron las batidoras?\n"
            f"(Si alguna no se enciende, lo dirás batidora por batidora.)\n\n"
            f"{base_cancel}"
        )
    if tipo == "funcionamiento_batidoras":
        return (
            f"**Reporte de Funcionamiento de Batidoras iniciado** por {user.mention}\n\n"
            f"**Batidora 1**\n\n{TXT_FUNCIONAMIENTO}\n\n{base_cancel}"
        )
    return (
        f"**Reporte de Apagado de Batidoras iniciado** por {user.mention}\n\n"
        f"**Batidora 1**\n\n{TXT_CHECKLIST_APAGADO}\n\n{base_cancel}"
    )
 
 
def _estado_inicial(tipo: str, user_id: int) -> dict:
    if tipo == "encendido_batidoras":
        return {
            "tipo": tipo,
            "user_id": user_id,
            "fase": "hora",
            "batidora": 1,
            "buffer": "",
            "answers": {},
            "videos_usados": [],
            "hora_global": None,
        }
    if tipo == "funcionamiento_batidoras":
        return {
            "tipo": tipo,
            "user_id": user_id,
            "fase": "funcionamiento",
            "batidora": 1,
            "buffer": "",
            "answers": {},
            "videos_usados": [],
            "hora_global": None,
        }
    # apagado
    return {
        "tipo": tipo,
        "user_id": user_id,
        "fase": "checklist",
        "batidora": 1,
        "buffer": "",
        "answers": {},
        "videos_usados": [],
        "hora_global": None,
    }
 
 
async def _iniciar(channel, user: discord.abc.User, tipo: str):
    channel_id = str(channel.id)
    if channel_id in conversation_state:
        dueño = conversation_state[channel_id]["user_id"]
        await channel.send(
            f"Ya hay un reporte de batidoras en curso (iniciado por <@{dueño}>). "
            f"Termínenlo o escriban **cancelar reporte**."
        )
        return
    conversation_state[channel_id] = _estado_inicial(tipo, user.id)
    await channel.send(_msg_inicio(tipo, user))
 
 
def _texto_checklist_para(state: dict) -> str:
    bat = state["batidora"]
    if state["tipo"] == "encendido_batidoras":
        return f"**Batidora {bat}**\n\n{TXT_CHECKLIST_ENCENDIDO}"
    if state["tipo"] == "apagado_batidoras":
        return f"**Batidora {bat}**\n\n{TXT_CHECKLIST_APAGADO}"
    return f"**Batidora {bat}**\n\n{TXT_FUNCIONAMIENTO}"
 
 
async def _ir_siguiente_batidora_o_fin(message: discord.Message, state: dict):
    channel_id = str(message.channel.id)
    bat = state["batidora"]
    tipo = state["tipo"]
 
    if bat < NUM_BATIDORAS:
        state["batidora"] = bat + 1
        state["buffer"] = ""
        if tipo == "funcionamiento_batidoras":
            state["fase"] = "funcionamiento"
            await message.channel.send(
                f"**Batidora {state['batidora']}**\n\n{TXT_FUNCIONAMIENTO}"
            )
        else:
            state["fase"] = "checklist"
            await message.channel.send(_texto_checklist_para(state))
        return
 
    nombres = {
        "encendido_batidoras": "Encendido de Batidoras",
        "apagado_batidoras": "Apagado de Batidoras",
        "funcionamiento_batidoras": "Funcionamiento de Batidoras",
    }
    await message.channel.send(
        f"✅ **Reporte de {nombres.get(tipo, 'Batidoras')} completado.** ¡Gracias!\n"
        f"_Respondido por <@{state['user_id']}>._"
    )
    print(f"[BATIDORAS OK] tipo={tipo} answers={json.dumps(state['answers'], ensure_ascii=False)}")
    del conversation_state[channel_id]
 
 
# ================== SLASH COMMANDS ==================
@bot.tree.command(
    name="reporte-encendido-batidoras",
    description="Inicia el Reporte de Encendido de Batidoras",
)
async def slash_encendido(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _iniciar(interaction.channel, interaction.user, "encendido_batidoras")
    try:
        await interaction.followup.send("Reporte de encendido iniciado en el canal.", ephemeral=True)
    except Exception:
        pass
 
 
@bot.tree.command(
    name="reporte-funcionamiento-batidoras",
    description="Inicia el Reporte de Funcionamiento de Batidoras",
)
async def slash_funcionamiento(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _iniciar(interaction.channel, interaction.user, "funcionamiento_batidoras")
    try:
        await interaction.followup.send("Reporte de funcionamiento iniciado en el canal.", ephemeral=True)
    except Exception:
        pass
 
 
@bot.tree.command(
    name="reporte-apagado-batidoras",
    description="Inicia el Reporte de Apagado de Batidoras",
)
async def slash_apagado(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await _iniciar(interaction.channel, interaction.user, "apagado_batidoras")
    try:
        await interaction.followup.send("Reporte de apagado iniciado en el canal.", ephemeral=True)
    except Exception:
        pass
 
 
@bot.event
async def on_ready():
    print(f"✅ Bot conectado como {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} slash commands sincronizados")
    except Exception as e:
        print(f"❌ Error al sincronizar comandos: {e}")
        traceback.print_exc()
 
 
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
 
    content_lower = (message.content or "").strip().lower()
    channel_id = str(message.channel.id)
    state = conversation_state.get(channel_id)
 
    # Cancelar
    if state and _parece_cancelar(content_lower):
        es_dueño = message.author.id == state["user_id"]
        if es_dueño or content_lower in {"cancelar reporte", "reporte cancelado", "canelar reporte"}:
            del conversation_state[channel_id]
            await message.channel.send("✅ Reporte de batidoras cancelado.")
        return
 
    # Inicio por texto
    inicios = {
        "reporte de encendido de batidoras": "encendido_batidoras",
        "iniciar reporte de encendido de batidoras": "encendido_batidoras",
        "reporte de funcionamiento de batidoras": "funcionamiento_batidoras",
        "iniciar reporte de funcionamiento de batidoras": "funcionamiento_batidoras",
        "reporte de apagado de batidoras": "apagado_batidoras",
        "iniciar reporte de apagado de batidoras": "apagado_batidoras",
    }
    if content_lower in inicios:
        await _iniciar(message.channel, message.author, inicios[content_lower])
        return
 
    if not state:
        return
 
    # Solo el dueño avanza el reporte
    if message.author.id != state["user_id"]:
        return
 
    await manejar_respuesta(message)
 
 
# ================== GROK ==================
async def consultar_grok(
    state: dict,
    respuesta_nueva: str,
    tiene_media: bool,
) -> dict:
    """
    Devuelve:
      respuesta_valida: bool
      es_fuera_de_servicio: bool   # no funciona / reparación / no se usó
      mensaje: str | null
    """
    tipo = state["tipo"]
    fase = state["fase"]
    bat = state["batidora"]
    buffer = state.get("buffer") or ""
 
    if fase == "hora":
        contexto_pregunta = (
            "Pregunta: ¿A qué hora se encendieron las batidoras?\n"
            "Válido: una hora o varias (ej. '7:20', 'La 3 a las 7:00 las demás a las 7:20').\n"
            "Inválido: punto, vacío, sin ninguna hora."
        )
    elif fase == "video":
        contexto_pregunta = (
            f"Fase: VIDEO del piñón de la batidora {bat}.\n"
            "Si hay video/imagen adjunta → válido.\n"
            "Si solo texto sin media → inválido. Mensaje: pide el video del piñón.\n"
            "No evalúes checklist aquí."
        )
    elif fase == "funcionamiento":
        contexto_pregunta = f"""
Fase: FUNCIONAMIENTO batidora {bat}.
Pide: temperatura del cabezote + si raspa bien la mezcla.
- Válido: temp (número o rango) + raspando sí/no o equivalente.
- Válido fuera de servicio: "no funciona", "no está en uso", "apagada", "en reparación".
- NO exijas copiar la pregunta. NO digas "confirma el encendido".
- Español de planta informal OK.
"""
    elif tipo == "encendido_batidoras":
        contexto_pregunta = f"""
Fase: CHECKLIST DE ENCENDIDO batidora {bat}.
Pide estado de: correas, chaveta, grasa/piñón, protección rodamiento, agua sal, ajuste piñón, dientes (completos o cuántos faltan), movimiento tambor.
REGLAS:
- NO es una pregunta de "¿está encendida?". Es revisión de estado ANTES/AL encender.
- NUNCA digas "te falta confirmar el encendido".
- Respuesta natural con varios puntos del checklist ES válida aunque no cubra el 100% palabra por palabra.
- Si reporta problemas (falta diente, grasa, correa floja) SIGUE siendo válida: es información útil, no un rechazo.
- Válido fuera de servicio: "no está en funcionamiento", "en reparación", "no se enciende", "batidora X no trabaja".
- "Todo bien" corto es débil: si es muy corto sin detalle, pide al menos 2-3 puntos del checklist O que diga si no funciona.
- Si el buffer + respuesta nueva ya tienen info suficiente de estado → válido.
- Inválido: solo ".", vacío, irrelevante.
- Sé razonable, no legalista.
"""
    else:  # apagado checklist
        contexto_pregunta = f"""
Fase: CHECKLIST DE APAGADO batidora {bat}.
Pide: dientes piñón (completos o faltantes, NO solo "igual" si puede ser más claro), cuchilla/protección, agua sal/grasa, horas encendido y apagado, tiempos de batida si aplicó.
REGLAS:
- Acepta respuestas de planta incompletas al 100% si traen lo esencial: estado del piñón/protección + horas + algún tiempo de batida.
- "Dientes igual" es aceptable si además trae horas y protección; si puedes, en mensaje (solo si inválida) pide especificar si faltan dientes.
- Válido fuera de servicio: "no funcionó", "no está en funcionamiento", "no se usó hoy".
- NO exijas cantidad exacta de "colores batidos" si no la dieron pero sí tiempos y horas.
- NO entres en bucle pidiendo lo que ya está en el buffer.
- Inválido: solo "." o basura.
"""
 
    system_prompt = """
Eres validador de reportes de batidoras en una fábrica de helados.
Responde ÚNICAMENTE JSON:
{
  "respuesta_valida": true o false,
  "es_fuera_de_servicio": true o false,
  "mensaje": null o "texto corto pidiendo SOLO lo que falta"
}
 
Principios:
1. Evalúa si la INFORMACIÓN esencial está (buffer + respuesta nueva), no si la frase es perfecta.
2. Typos y español informal OK ("se pagado", "hechar grasa", "iso").
3. es_fuera_de_servicio=true si dice que no funciona / reparación / no se usó / no se encendió.
4. Si es_fuera_de_servicio y se entiende → respuesta_valida=true.
5. Nunca uses el mensaje genérico "Tu mensaje está incompleto" sin decir qué falta.
6. Nunca pidas "confirmar el encendido" en la fase de checklist de estado.
7. temperature-level: sé práctico para operarios de planta, no un auditor legal.
 
Errores reales que DEBES evitar:
- Rechazar un checklist detallado (correas, grasa, dientes, agua sal) como "incompleto".
- Aceptar un punto "." como respuesta.
- Confundir checklist de estado con confirmación de encendido.
- Pedir video cuando dijeron que la máquina no funciona (eso lo maneja el código con es_fuera_de_servicio).
- Exigir copiar toda la lista del prompt.
"""
 
    user_content = f"""
Tipo de reporte: {tipo}
Fase actual: {fase}
Batidora actual: {bat}
Tiene_media_en_este_mensaje: {tiene_media}
 
{contexto_pregunta}
 
BUFFER de la misma fase (intentos previos):
\"\"\"{buffer or "(vacío)"}\"\"\"
 
Respuesta NUEVA:
\"\"\"{respuesta_nueva}\"\"\"
 
Evalúa BUFFER + respuesta nueva juntos.
"""
 
    try:
        response = client.chat.completions.create(
            model="grok-4-1-fast",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        return {
            "respuesta_valida": bool(data.get("respuesta_valida", False)),
            "es_fuera_de_servicio": bool(data.get("es_fuera_de_servicio", False)),
            "mensaje": data.get("mensaje"),
        }
    except Exception as e:
        print(f"Error al consultar Grok: {e}")
        traceback.print_exc()
        if _vacio_o_basura(respuesta_nueva, tiene_media):
            return {
                "respuesta_valida": False,
                "es_fuera_de_servicio": False,
                "mensaje": "Escribe la respuesta con datos (no uses solo un punto).",
            }
        # Fail-open con texto real
        return {
            "respuesta_valida": True,
            "es_fuera_de_servicio": bool(
                re.search(
                    r"no\s+(est[aá]|funciona|se\s+us|enciend)|reparaci[oó]n|fuera\s+de\s+servicio",
                    respuesta_nueva,
                    re.I,
                )
            ),
            "mensaje": None,
        }
 
 
# ================== MANEJO ==================
async def manejar_respuesta(message: discord.Message):
    channel_id = str(message.channel.id)
    state = conversation_state.get(channel_id)
    if not state:
        return
 
    texto = (message.content or "").strip()
    tiene_media = _tiene_media(message)
    fase = state["fase"]
    bat = state["batidora"]
    tipo = state["tipo"]
 
    # ---- FASE VIDEO: exigir media de verdad ----
    if fase == "video":
        if not tiene_media:
            if _vacio_o_basura(texto, False):
                await message.channel.send(
                    f"Necesito el **video del piñón de la batidora {bat}**, no un punto. "
                    f"Graba y envíalo aquí."
                )
            else:
                # Texto sin video: no avanzar
                await message.channel.send(
                    f"Anota eso, pero aún falta el **video del piñón de la batidora {bat}**. Envíalo por favor."
                )
                if texto:
                    state["buffer"] = ((state.get("buffer") or "") + "\n" + texto).strip()
            return
 
        # Detectar reuso del mismo archivo
        ids = _media_ids(message)
        reusado = [i for i in ids if i in state.get("videos_usados", [])]
        if reusado:
            await message.channel.send(
                f"⚠️ Ese video/archivo ya se usó en otra batidora. "
                f"Envía un **video nuevo del piñón de la batidora {bat}**."
            )
            return
 
        # Video OK
        try:
            await message.add_reaction("✅")
        except Exception:
            pass
 
        key = f"{tipo}_bat{bat}_video"
        state["answers"][key] = {
            "archivos": ids,
            "nota": texto or state.get("buffer") or "",
        }
        state["videos_usados"].extend(ids)
        state["buffer"] = ""
        await _ir_siguiente_batidora_o_fin(message, state)
        return
 
    # ---- Prefiltro basura (no video) ----
    if _vacio_o_basura(texto, tiene_media):
        await message.channel.send(
            "Escribe la respuesta con datos (no uses solo un punto). "
            "Si la batidora no funciona, dilo con palabras: *no está en funcionamiento*."
        )
        return
 
    # Media en fase checklist no sustituye el texto del checklist
    # (el video se pide después). Si mandan solo media en checklist, avisar.
    if tiene_media and not texto and fase in {"checklist", "hora", "funcionamiento"}:
        await message.channel.send(
            "Primero escribe el **estado/datos** en texto. "
            "El video del piñón te lo pediré en el siguiente paso (si la máquina está en uso)."
        )
        return
 
    respuesta_para_ia = texto
    if tiene_media:
        nombres = ", ".join(a.filename for a in message.attachments)
        respuesta_para_ia = f"{texto}\n[Adjuntos: {nombres}]".strip()
 
    buffer_prev = state.get("buffer") or ""
    buffer_completo = (buffer_prev + "\n" + texto).strip() if buffer_prev else texto
 
    decision = await consultar_grok(state, respuesta_para_ia, tiene_media)
 
    if not decision.get("respuesta_valida", False):
        state["buffer"] = buffer_completo
        msg = decision.get("mensaje") or "Falta un dato. Completa solo lo que falta."
        # Neutralizar genéricos tóxicos
        if msg.strip().lower() in {
            "tu mensaje está incompleto.",
            "tu mensaje está incompleto",
            "tu respuesta está incompleta. di exactamente lo que se te pidió.",
            "responde completo lo que se te preguntó.",
        }:
            msg = "Me falta un dato concreto. Complétalo en un mensaje más (o di si la batidora no funciona)."
        await message.channel.send(msg)
        return
 
    # ===== Válida =====
    try:
        await message.add_reaction("✅")
    except Exception:
        pass
 
    fuera = decision.get("es_fuera_de_servicio", False)
 
    # --- Hora global (solo encendido) ---
    if fase == "hora":
        state["hora_global"] = buffer_completo
        state["answers"]["hora_encendido"] = buffer_completo
        state["buffer"] = ""
        state["fase"] = "checklist"
        state["batidora"] = 1
        await message.channel.send(_texto_checklist_para(state))
        return
 
    # --- Funcionamiento ---
    if fase == "funcionamiento":
        state["answers"][f"func_bat{bat}"] = buffer_completo
        state["buffer"] = ""
        await _ir_siguiente_batidora_o_fin(message, state)
        return
 
    # --- Checklist encendido / apagado ---
    if fase == "checklist":
        state["answers"][f"{tipo}_bat{bat}_checklist"] = buffer_completo
        state["buffer"] = ""
 
        if fuera:
            # Sin video si no funciona / no se usó
            state["answers"][f"{tipo}_bat{bat}_video"] = "N/A — fuera de servicio / no usada"
            await message.channel.send(
                f"Batidora {bat} marcada como **no en uso / fuera de servicio**. Siguiente…"
            )
            await _ir_siguiente_batidora_o_fin(message, state)
            return
 
        # Siempre pedir video si está en uso (regla fija — corrige el bug de saltarse videos)
        state["fase"] = "video"
        await message.channel.send(TXT_VIDEO.format(bat=bat))
        return
 
 
# ================== INICIO ==================
bot.run(DISCORD_TOKEN)

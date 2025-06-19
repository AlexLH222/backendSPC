import os
import json
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from typing import Dict
import firebase_admin
from firebase_admin import credentials, firestore
import re
import logging
from datetime import datetime

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de Gemini AI
try:
    genai.configure(api_key=os.getenv("API_KEY"))
    modelo = genai.GenerativeModel("gemini-1.5-flash")
    logger.info("Gemini AI configurado correctamente")
except Exception as e:
    logger.error(f"Error configurando Gemini AI: {e}")
    raise

# Inicializar Firebase
try:
    if not firebase_admin._apps:
        firebase_config = os.getenv("FIREBASE_CONFIG")
        if firebase_config:
            cred = credentials.Certificate(json.loads(firebase_config))
        elif os.path.exists("clave_firebase.json"):
            cred = credentials.Certificate("clave_firebase.json")
        else:
            raise ValueError("No se encontró configuración para Firebase")
        
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    logger.info("Firebase inicializado correctamente")
except Exception as e:
    logger.error(f"Error inicializando Firebase: {e}")
    raise

app = FastAPI(
    title="API Coprodelito",
    description="Asistente emocional para estudiantes",
    version="1.0",
    docs_url="/docs",
    redoc_url=None
)

# Configuración CORS mejorada
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Variables de estado (para producción considera usar Redis)
class EstadoConversacion:
    def __init__(self):
        self.historial = []
        self.emociones = set()
        self.situaciones = []
        self.correo_alumno = None
        self.documento_id = None
        self.primer_mensaje = None

estado = EstadoConversacion()

# Modelos Pydantic
class ChatRequest(BaseModel):
    message: str

class UserRequest(BaseModel):
    email: str
    password: str

# Endpoints
@app.get("/", include_in_schema=False)
async def root():
    """Endpoint raíz que redirige a la documentación"""
    return RedirectResponse(url="/docs")

@app.get("/health")
async def health_check():
    """Endpoint de verificación de salud"""
    return {
        "status": "healthy",
        "version": "1.0",
        "timestamp": datetime.now().isoformat()
    }

@app.post("/register", response_model=Dict)
async def register(user: UserRequest):
    """Registro de nuevos usuarios"""
    correo = user.email.lower().strip()
    password = user.password.strip()

    # Validación de formato de correo
    if not re.match(r'^[a-z]+\.[a-z]+@spc\.edu\.pe$', correo):
        raise HTTPException(
            status_code=400,
            detail="El correo debe tener el formato nombre.apellido@spc.edu.pe"
        )

    # Validación de contraseña
    if len(password) != 8:
        raise HTTPException(
            status_code=400,
            detail="La contraseña debe tener 8 caracteres."
        )

    try:
        # Verificar si el usuario ya existe
        usuarios_ref = db.collection("correosEstudiantes")
        coincidencias = usuarios_ref.where("correoEstudiante", "==", correo).limit(1).get()
        
        if coincidencias:
            raise HTTPException(
                status_code=400,
                detail="El correo ya está registrado."
            )

        # Crear nuevo usuario
        await usuarios_ref.add({
            "correoEstudiante": correo,
            "pswEstudiante": password,
            "fechaRegistro": firestore.SERVER_TIMESTAMP
        })
        
        logger.info(f"Usuario registrado: {correo}")
        return {"success": True, "user_id": correo}
        
    except Exception as e:
        logger.error(f"Error en registro: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error interno del servidor"
        )

@app.post("/login", response_model=Dict)
async def login(user: UserRequest):
    """Autenticación de usuarios"""
    correo = user.email.lower().strip()
    password = user.password.strip()

    try:
        usuarios_ref = db.collection("correosEstudiantes")
        query = usuarios_ref.where("correoEstudiante", "==", correo) \
                          .where("pswEstudiante", "==", password) \
                          .limit(1)
        
        coincidencias = await query.get()
        
        if not coincidencias:
            logger.warning(f"Intento de login fallido para: {correo}")
            raise HTTPException(
                status_code=401,
                detail="Credenciales incorrectas"
            )
            
        logger.info(f"Login exitoso para: {correo}")
        return {"success": True, "user_id": correo}
        
    except Exception as e:
        logger.error(f"Error en login: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error interno del servidor"
        )

@app.post("/welcome", response_model=Dict)
async def mensaje_bienvenida(user: UserRequest):
    """Mensaje de bienvenida inicial"""
    try:
        nombre = user.email.split('@')[0].replace('.', ' ')
        nombre_cap = ' '.join([p.capitalize() for p in nombre.split()])
        mensaje = f"¡Hola {nombre_cap}! 👋 Soy Coprodelito, tu asistente emocional. ¿Cómo te sientes hoy?"
        
        # Reiniciar estado de conversación
        estado.historial = [{"role": "assistant", "parts": [mensaje]}]
        estado.emociones = set()
        estado.situaciones = []
        estado.correo_alumno = user.email.lower().strip()
        estado.documento_id = None
        estado.primer_mensaje = None
        
        logger.info(f"Nueva conversación iniciada para: {estado.correo_alumno}")
        return {"response": mensaje}
        
    except Exception as e:
        logger.error(f"Error en bienvenida: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error al generar mensaje de bienvenida"
        )

@app.post("/chat", response_model=Dict)
async def chat_endpoint(chat: ChatRequest):
    """Endpoint principal del chatbot"""
    try:
        respuesta = await generar_respuesta_emocional(chat.message)
        return {"response": respuesta}
    except Exception as e:
        logger.error(f"Error en chat: {e}")
        raise HTTPException(
            status_code=500,
            detail="Error al procesar el mensaje"
        )

# Funciones auxiliares
def es_agradecimiento(texto: str) -> bool:
    """Detecta si el mensaje es un agradecimiento"""
    palabras_clave = ["gracias", "muchas gracias", "agradecido", "agradecida"]
    return any(palabra in texto.lower() for palabra in palabras_clave)

def necesita_recomendaciones(texto: str) -> bool:
    """Detecta si el mensaje solicita recomendaciones"""
    palabras_clave = ["consejos", "tips", "recomendación", "qué hago", "no sé", "ayúdame"]
    return any(p in texto.lower() for p in palabras_clave)

def es_cambio_tema() -> bool:
    """Determina si hay un cambio de tema en la conversación"""
    if len(estado.historial) < 2:
        return True
        
    ultimos_mensajes = [m['parts'][0].lower() for m in estado.historial[-3:] if m['role'] == 'user']
    conectores = ["y", "además", "también", "pero", "aunque", "luego"]
    return not any(conector in ' '.join(ultimos_mensajes) for conector in conectores)

async def generar_respuesta_emocional(mensaje_usuario: str) -> str:
    """Genera una respuesta emocional usando Gemini AI"""
    try:
        if es_agradecimiento(mensaje_usuario):
            return "¡De nada! 😊 Aquí estaré cuando me necesites."

        estado.historial.append({"role": "user", "parts": [mensaje_usuario]})

        if estado.primer_mensaje is None:
            estado.primer_mensaje = mensaje_usuario

        contexto = "\n".join([f"{m['role']}: {m['parts'][0]}" for m in estado.historial[-5:]])
        
        prompt = f"""
Eres Coprodelito, un asistente emocional para estudiantes. Contexto previo:
{contexto}

Nuevo mensaje: "{mensaje_usuario}"

Responde de forma empática y natural, identificando emociones cuando sea relevante.
"""
        respuesta = await modelo.generate_content_async(prompt)
        texto = respuesta.text.strip()

        # Detección de emociones
        emocion_detectada = ""
        if es_cambio_tema() and not texto.lower().startswith("emoción detectada"):
            emocion_respuesta = await modelo.generate_content_async(
                f"Identifica la emoción principal en: '{mensaje_usuario}'. Responde solo con una palabra."
            )
            emocion_detectada = emocion_respuesta.text.strip()
            texto = f"Emoción detectada: {emocion_detectada} 😊\n{texto}"

        # Formateo de recomendaciones
        if necesita_recomendaciones(mensaje_usuario) and "🔹" not in texto:
            lineas = [line.strip() for line in texto.split('\n') if line.strip()]
            texto = "\n".join([f"🔹 {l}" for l in lineas[:3]])

        await guardar_emocion_firestore(emocion_detectada, mensaje_usuario)
        
        estado.historial.append({"role": "assistant", "parts": [texto]})
        return texto

    except Exception as e:
        logger.error(f"Error generando respuesta: {e}")
        return "¡Vaya! Algo no ha ido bien. ¿Podrías intentarlo de nuevo?"

async def guardar_emocion_firestore(emocion: str, mensaje: str):
    """Guarda las emociones detectadas en Firestore"""
    if not emocion or not estado.correo_alumno:
        return

    try:
        emocion = emocion.strip().capitalize()
        if emocion.lower() not in {e.lower() for e in estado.emociones}:
            estado.emociones.add(emocion)
            estado.situaciones.append(mensaje)

            data = {
                "alumno": estado.correo_alumno,
                "emociones": list(estado.emociones),
                "situacion": estado.situaciones,
                "ultimaActualizacion": firestore.SERVER_TIMESTAMP
            }

            if estado.documento_id:
                await db.collection("emocionesDetectadas").document(estado.documento_id).update(data)
            else:
                doc_ref = await db.collection("emocionesDetectadas").add(data)
                estado.documento_id = doc_ref.id
                
            logger.info(f"Emoción guardada: {emocion} para {estado.correo_alumno}")
    except Exception as e:
        logger.error(f"Error guardando emoción: {e}")

# Manejo especial para solicitudes OPTIONS
@app.options("/{rest_of_path:path}")
async def options_handler():
    return JSONResponse(status_code=200)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        timeout_keep_alive=30,
        reload=False
    )

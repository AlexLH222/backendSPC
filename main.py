import os
import json
from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from typing import Dict
import firebase_admin
from firebase_admin import credentials, firestore
import re
import logging
from datetime import datetime

# Configuración mejorada de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('api.log')
    ]
)
logger = logging.getLogger(__name__)

# Configuración de Gemini mejorada
# Configuración CORREGIDA de Gemini
try:
    API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("API_KEY")
    if not API_KEY:
        logger.error("API_KEY no configurada en variables de entorno")
        raise ValueError("API_KEY no configurada")
    
    genai.configure(
        api_key=API_KEY,
        transport='rest',  # Mantenemos esto para mayor estabilidad
        # Eliminamos el timeout de client_options
        client_options={
            'api_endpoint': 'https://generativelanguage.googleapis.com'
        }
    )
    
    modelo = genai.GenerativeModel(
        "gemini-1.5-flash",
        generation_config={
            "temperature": 0.7,
            "top_p": 0.9
        }
    )
    logger.info("Gemini configurado correctamente")
except Exception as e:
    logger.error(f"Error configurando Gemini: {str(e)}")
    modelo = None

# Inicialización de Firebase con serviceAccountKey.json
try:
    if not firebase_admin._apps:
        # Opción 1: Variable de entorno (para Render)
        firebase_config = os.getenv("FIREBASE_CONFIG")
        if firebase_config:
            cred = credentials.Certificate(json.loads(firebase_config))
        # Opción 2: Archivo serviceAccountKey.json (local)
        elif os.path.exists("serviceAccountKey.json"):
            cred = credentials.Certificate("serviceAccountKey.json")
        else:
            logger.error("No se encontró serviceAccountKey.json ni FIREBASE_CONFIG")
            raise ValueError("Configuración de Firebase no encontrada")
        
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    logger.info("Firebase inicializado correctamente con serviceAccountKey.json")
except Exception as e:
    logger.error(f"Error inicializando Firebase: {str(e)}")
    db = None

app = FastAPI()

# Configuración CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Variables globales (se mantienen igual)
usuarios = {}
historial_conversacion = []
emociones_detectadas = set()
situaciones_emocionales = []
correo_alumno = None
documento_emocion_id = None
primer_mensaje = None

# Modelos Pydantic
class ChatRequest(BaseModel):
    message: str

class UserRequest(BaseModel):
    email: str
    password: str

# Endpoints
@app.post("/register")
def register(user: UserRequest) -> Dict:
    correo = user.email.lower().strip()
    password = user.password.strip()

    if not re.match(r'^[a-z]+\.[a-z]+@spc\.edu\.pe$', correo):
        return {"success": False, "error": "El correo debe tener el formato nombre.apellido@spc.edu.pe"}

    if len(password) != 8:
        return {"success": False, "error": "La contraseña debe tener 8 caracteres."}

    usuarios_ref = db.collection("correosEstudiantes")
    coincidencias = usuarios_ref.where("correoEstudiante", "==", correo).stream()
    if any(coincidencias):
        return {"success": False, "error": "El correo ya está registrado."}

    usuarios_ref.add({
        "correoEstudiante": correo,
        "pswEstudiante": password,
        "fecha_registro": firestore.SERVER_TIMESTAMP
    })
    return {"success": True, "user_id": correo}

@app.post("/login")
def login(user: UserRequest) -> Dict:
    correo = user.email.lower().strip()
    password = user.password.strip()

    usuarios_ref = db.collection("correosEstudiantes")
    coincidencias = usuarios_ref.where("correoEstudiante", "==", correo).where("pswEstudiante", "==", password).stream()

    if any(coincidencias):
        return {"success": True, "user_id": correo}
    else:
        return {"success": False, "error": "Credenciales incorrectas"}

@app.post("/welcome")
def mensaje_bienvenida(user: UserRequest):
    global historial_conversacion, primer_mensaje, emociones_detectadas, correo_alumno, documento_emocion_id, situaciones_emocionales
    nombre = user.email.split('@')[0].replace('.', ' ')
    nombre_cap = ' '.join([p.capitalize() for p in nombre.split()])
    mensaje = f"¡Hola {nombre_cap}! 👋 Soy Coprodelito, tu asistente emocional. ¿Cómo te sientes hoy?"
    historial_conversacion.clear()
    emociones_detectadas.clear()
    situaciones_emocionales.clear()
    documento_emocion_id = None
    correo_alumno = user.email.lower().strip()
    primer_mensaje = None
    historial_conversacion.append({"role": "assistant", "parts": [mensaje]})
    return {"response": mensaje}

# Funciones auxiliares
def es_agradecimiento(texto):
    return any(palabra in texto.lower() for palabra in ["gracias", "muchas gracias", "agradecido", "agradecida"])

def necesita_recomendaciones(texto):
    return any(p in texto.lower() for p in ["consejos", "tips", "recomendación", "qué hago", "no sé", "ayúdame"])

def es_cambio_tema():
    if len(historial_conversacion) < 2:
        return True
    ultimos = [m['parts'][0].lower() for m in historial_conversacion[-3:] if m['role'] == 'user']
    conectores = ["y", "además", "también", "pero", "aunque", "luego"]
    return not any(con in ' '.join(ultimos) for con in conectores)

# Generar respuesta emocional
def generar_respuesta_emocional(mensaje_usuario: str):
    global primer_mensaje, documento_emocion_id

    try:
        if es_agradecimiento(mensaje_usuario):
            return "¡De nada! 😊 Aquí estaré cuando me necesites."

        historial_conversacion.append({"role": "user", "parts": [mensaje_usuario]})

        if primer_mensaje is None:
            primer_mensaje = mensaje_usuario

        cambio_tema = es_cambio_tema()
        quiere_tips = necesita_recomendaciones(mensaje_usuario)

        contexto = "\n".join([f"{m['role']}: {m['parts'][0]}" for m in historial_conversacion[-5:]])

        prompt = f"""
Eres Coprodelito, un asistente emocional para jóvenes. Debes conversar como un amigo empático.

Contexto de la conversación:
{contexto}

Mensaje nuevo del usuario:
"{mensaje_usuario}"

REGLAS:
1. Si es NUEVO TEMA, empieza con: "Emoción detectada: [emoción] [emoji]"
   - Luego expresa 1 o 2 frases emocionales cercanas.
   - Termina con una pregunta emocional y natural.

2. Si el usuario quiere CONSEJOS, responde con:
   🔹 Consejo 1
   🔹 Consejo 2
   🔹 Consejo 3

3. Si es continuación, responde como un amigo que sigue el hilo:
   - Usa emojis, lenguaje cálido y simple.
   - Sigue el tema anterior y anima al usuario.

NO SALGAS DEL PERSONAJE. NO SEAS ROBÓTICO. SÉ CERCANO Y HUMANO.

Responde:
"""
        respuesta = modelo.generate_content(prompt)
        texto = respuesta.text.strip()

        if cambio_tema and not texto.lower().startswith("emoción detectada"):
            emocion_detectada = modelo.generate_content(
                f"¿Qué emoción expresa esta frase: '{mensaje_usuario}'? "
                f"Responde solo con una emoción como 'Alegría', 'Tristeza', etc."
            ).text.strip()
            texto = f"Emoción detectada: {emocion_detectada} 😊\n{texto}"
        else:
            emocion_match = re.search(r"Emoción detectada: ([\wÁÉÍÓÚñáéíóú]+)", texto)
            if emocion_match:
                emocion_detectada = emocion_match.group(1).strip()
            else:
                emocion_detectada = None

        if quiere_tips and "🔹" not in texto:
            lineas = [line.strip() for line in texto.split('\n') if line.strip()]
            texto = "\n".join([f"🔹 {l}" for l in lineas[:3]])

        # Guardar emociones y situaciones en Firestore
        if emocion_detectada and db:
            if emocion_detectada.lower() not in map(str.lower, emociones_detectadas):
                emociones_detectadas.add(emocion_detectada)
                situaciones_emocionales.append(mensaje_usuario)

                if documento_emocion_id is None:
                    doc_ref = db.collection("emocionesDetectadas").document()
                    doc_ref.set({
                        "alumno": correo_alumno,
                        "emociones": list(emociones_detectadas),
                        "situacion": situaciones_emocionales,
                        "fechaHora": firestore.SERVER_TIMESTAMP
                    })
                    documento_emocion_id = doc_ref.id
                else:
                    doc_ref = db.collection("emocionesDetectadas").document(documento_emocion_id)
                    doc_ref.update({
                        "emociones": firestore.ArrayUnion([emocion_detectada]),
                        "situacion": firestore.ArrayUnion([mensaje_usuario])
                    })

        historial_conversacion.append({"role": "assistant", "parts": [texto]})
        return texto

    except Exception as e:
        logger.error(f"Error generando respuesta: {str(e)}")
        return "¡Uy! Algo salió mal 😥. ¿Puedes intentarlo otra vez?"

@app.post("/chat")
def chat_endpoint(chat: ChatRequest):
    respuesta = generar_respuesta_emocional(chat.message)
    return {"response": respuesta}

# Endpoint adicional para Render
@app.get("/health")
def health_check():
    return {
        "status": "running",
        "services": {
            "firebase": db is not None,
            "gemini": modelo is not None
        },
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        timeout_keep_alive=30
    )

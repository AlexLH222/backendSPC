import os
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
import google.generativeai as genai
from typing import Dict, Optional
import firebase_admin
from firebase_admin import credentials, firestore
import re
import logging
from datetime import datetime
import asyncio

# Configuración de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Configuración de Gemini AI
try:
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    if not GEMINI_API_KEY:
        raise ValueError("API_KEY no configurada")
    
    genai.configure(
        api_key=GEMINI_API_KEY,
        transport='rest',
        client_options={
            'api_endpoint': 'https://generativelanguage.googleapis.com'
        }
    )
    
    modelo = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("Gemini configurado correctamente")
except Exception as e:
    logger.error(f"Error configurando Gemini: {str(e)}")
    modelo = None

# Inicialización de Firebase
try:
    if not firebase_admin._apps:
        # Opción 1: Variables de entorno en Render
        firebase_config = os.getenv("FIREBASE_CONFIG")
        if firebase_config:
            cred = credentials.Certificate(json.loads(firebase_config))
        # Opción 2: Archivo JSON subido a Render
        elif os.path.exists("serviceAccountKey.json"):
            cred = credentials.Certificate("serviceAccountKey.json")
        else:
            raise ValueError("No se encontró configuración para Firebase")
        
        firebase_admin.initialize_app(cred)
    
    db = firestore.client()
    logger.info("Firebase inicializado correctamente")
except Exception as e:
    logger.error(f"Error inicializando Firebase: {str(e)}")
    db = None

app = FastAPI(
    title="API Coprodelito",
    description="Asistente emocional para estudiantes",
    version="1.0"
)

# Configuración CORS para Flutter
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelo de estado de conversación
class ConversationState:
    def __init__(self):
        self.reset()
    
    def reset(self):
        self.history = []
        self.emotions = set()
        self.situations = []
        self.student_email = None
        self.doc_id = None
        self.first_message = None
        self.lock = asyncio.Lock()

state = ConversationState()

# Modelos Pydantic
class ChatRequest(BaseModel):
    message: str

class UserRequest(BaseModel):
    email: str
    password: str

# Funciones auxiliares
def validate_email(email: str) -> str:
    email = email.lower().strip()
    if not re.match(r'^[a-z]+\.[a-z]+@spc\.edu\.pe$', email):
        raise ValueError("El correo debe tener el formato nombre.apellido@spc.edu.pe")
    return email

def is_thanks(text: str) -> bool:
    return any(palabra in text.lower() for palabra in ["gracias", "muchas gracias", "agradecido", "agradecida"])

def needs_advice(text: str) -> bool:
    return any(p in text.lower() for p in ["consejos", "tips", "recomendación", "qué hago", "no sé", "ayúdame"])

def is_topic_change() -> bool:
    if len(state.history) < 2:
        return True
    last_messages = [msg['parts'][0].lower() for msg in state.history[-3:] if msg['role'] == 'user']
    connectors = ["y", "además", "también", "pero", "aunque", "luego"]
    return not any(conn in ' '.join(last_messages) for conn in connectors)

# Endpoints
@app.post("/register")
async def register(user: UserRequest) -> Dict:
    try:
        email = validate_email(user.email)
        password = user.password.strip()

        if len(password) != 8:
            raise HTTPException(status_code=400, detail="La contraseña debe tener 8 caracteres")

        if not db:
            raise HTTPException(status_code=500, detail="Error de base de datos")

        users_ref = db.collection("correosEstudiantes")
        query = users_ref.where("correoEstudiante", "==", email).limit(1)
        docs = query.get()

        if any(docs):
            raise HTTPException(status_code=400, detail="El correo ya está registrado")

        await users_ref.add({
            "correoEstudiante": email,
            "pswEstudiante": password,
            "created_at": firestore.SERVER_TIMESTAMP
        })

        return {"success": True, "user_id": email}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error en registro: {str(e)}")
        raise HTTPException(status_code=500, detail="Error en el servidor")

@app.post("/login")
async def login(user: UserRequest) -> Dict:
    try:
        email = validate_email(user.email)
        password = user.password.strip()

        if not db:
            raise HTTPException(status_code=500, detail="Error de base de datos")

        users_ref = db.collection("correosEstudiantes")
        query = users_ref.where("correoEstudiante", "==", email) \
                        .where("pswEstudiante", "==", password) \
                        .limit(1)
        docs = query.get()

        if not any(docs):
            raise HTTPException(status_code=401, detail="Credenciales incorrectas")

        return {"success": True, "user_id": email}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error en login: {str(e)}")
        raise HTTPException(status_code=500, detail="Error en el servidor")

@app.post("/welcome")
async def welcome(user: UserRequest):
    try:
        email = validate_email(user.email)
        name = email.split('@')[0].replace('.', ' ')
        name_cap = ' '.join([p.capitalize() for p in name.split()])
        message = f"¡Hola {name_cap}! 👋 Soy Coprodelito, tu asistente emocional. ¿Cómo te sientes hoy?"

        async with state.lock:
            state.reset()
            state.student_email = email
            state.history = [{"role": "assistant", "parts": [message]}]

        return {"response": message}

    except Exception as e:
        logger.error(f"Error en bienvenida: {str(e)}")
        raise HTTPException(status_code=500, detail="Error al generar bienvenida")

@app.post("/chat")
async def chat_endpoint(chat: ChatRequest):
    if not modelo:
        raise HTTPException(status_code=503, detail="Servicio de IA no disponible")

    try:
        response = await generate_emotional_response(chat.message)
        return {"response": response}
    except Exception as e:
        logger.error(f"Error en chat: {str(e)}")
        raise HTTPException(status_code=500, detail="Error procesando mensaje")

async def generate_emotional_response(user_message: str) -> str:
    async with state.lock:
        if is_thanks(user_message):
            return "¡De nada! 😊 Aquí estaré cuando me necesites."

        state.history.append({"role": "user", "parts": [user_message]})

        if not state.first_message:
            state.first_message = user_message

        topic_change = is_topic_change()
        needs_advice_flag = needs_advice(user_message)

        context = "\n".join([f"{msg['role']}: {msg['parts'][0]}" for msg in state.history[-5:]])

        prompt = f"""
Eres Coprodelito, un asistente emocional para jóvenes. Debes conversar como un amigo empático.

Contexto de la conversación:
{context}

Mensaje nuevo del usuario:
"{user_message}"

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
        try:
            response = await modelo.generate_content_async(prompt)
            text = response.text.strip()

            if topic_change and not text.lower().startswith("emoción detectada"):
                emotion_response = await modelo.generate_content_async(
                    f"¿Qué emoción expresa esta frase: '{user_message}'? "
                    f"Responde solo con una emoción como 'Alegría', 'Tristeza', etc."
                )
                detected_emotion = emotion_response.text.strip()
                text = f"Emoción detectada: {detected_emotion} 😊\n{text}"
            else:
                emotion_match = re.search(r"Emoción detectada: ([\wÁÉÍÓÚñáéíóú]+)", text)
                detected_emotion = emotion_match.group(1).strip() if emotion_match else None

            if needs_advice_flag and "🔹" not in text:
                lines = [line.strip() for line in text.split('\n') if line.strip()]
                text = "\n".join([f"🔹 {line}" for line in lines[:3]])

            # Guardar emociones en Firestore
            if detected_emotion and db:
                if detected_emotion.lower() not in map(str.lower, state.emotions):
                    state.emotions.add(detected_emotion)
                    state.situations.append(user_message)

                    if state.doc_id is None:
                        doc_ref = db.collection("emocionesDetectadas").document()
                        doc_ref.set({
                            "alumno": state.student_email,
                            "emociones": list(state.emotions),
                            "situacion": state.situations,
                            "fechaHora": firestore.SERVER_TIMESTAMP
                        })
                        state.doc_id = doc_ref.id
                    else:
                        doc_ref = db.collection("emocionesDetectadas").document(state.doc_id)
                        doc_ref.update({
                            "emociones": firestore.ArrayUnion([detected_emotion]),
                            "situacion": firestore.ArrayUnion([user_message])
                        })

            state.history.append({"role": "assistant", "parts": [text]})
            return text

        except Exception as e:
            logger.error(f"Error generando respuesta: {str(e)}")
            return "¡Uy! Algo salió mal 😥. ¿Puedes intentarlo otra vez?"

@app.get("/health")
async def health_check():
    return {
        "status": "running",
        "services": {
            "firebase": bool(db),
            "gemini": bool(modelo)
        },
        "timestamp": datetime.now().isoformat()
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)

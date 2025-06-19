import os
import json
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
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

# Configuración de Gemini AI con manejo robusto de errores
try:
    GEMINI_API_KEY = os.getenv("API_KEY")
    if not GEMINI_API_KEY:
        raise ValueError("API_KEY no está configurada")
    
    genai.configure(api_key=GEMINI_API_KEY)
    modelo = genai.GenerativeModel("gemini-1.5-flash")
    
    # Verificación temprana de conexión
    async def verify_gemini():
        try:
            await modelo.generate_content_async("Test connection", timeout=10)
            logger.info("Conexión con Gemini verificada")
            return True
        except Exception as e:
            logger.error(f"Error verificando Gemini: {str(e)}")
            return False
    
    asyncio.create_task(verify_gemini())
    
except Exception as e:
    logger.error(f"Error crítico al configurar Gemini: {str(e)}")
    modelo = None

# Inicialización de Firebase con manejo mejorado de errores
def init_firebase():
    try:
        if not firebase_admin._apps:
            firebase_config = os.getenv("FIREBASE_CONFIG")
            if firebase_config:
                try:
                    config_dict = json.loads(firebase_config)
                    cred = credentials.Certificate(config_dict)
                except json.JSONDecodeError:
                    logger.error("Error decodificando FIREBASE_CONFIG JSON")
                    raise
            elif os.path.exists("serviceAccountKey.json"):
                cred = credentials.Certificate("serviceAccountKey.json")
            else:
                raise ValueError("No se encontró configuración para Firebase")
            
            firebase_admin.initialize_app(cred)
        
        return firestore.client()
    except Exception as e:
        logger.error(f"Error inicializando Firebase: {str(e)}")
        raise

try:
    db = init_firebase()
    logger.info("Firebase inicializado correctamente")
except Exception:
    db = None

app = FastAPI(
    title="API Coprodelito",
    description="Asistente emocional para estudiantes",
    version="2.0",
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json"
)

# Middleware CORS mejorado
@app.middleware("http")
async def add_cors_header(request: Request, call_next):
    response = await call_next(request)
    response.headers.update({
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Expose-Headers": "*"
    })
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Modelo de estado mejorado
class ConversationState:
    __instance = None
    
    def __new__(cls):
        if cls.__instance is None:
            cls.__instance = super().__new__(cls)
            cls.__instance.reset()
        return cls.__instance
    
    def reset(self):
        self.history = []
        self.emotions = set()
        self.situations = []
        self.student_email = None
        self.doc_id = None
        self.first_message = None
        self.lock = asyncio.Lock()

state = ConversationState()

# Modelos Pydantic con validaciones mejoradas
class ChatRequest(BaseModel):
    message: str
    
    @classmethod
    def validate_message(cls, v):
        if not v or len(v.strip()) < 1:
            raise ValueError("El mensaje no puede estar vacío")
        return v.strip()

class UserRequest(BaseModel):
    email: str
    password: str
    
    @classmethod
    def validate_email(cls, v):
        if not re.match(r'^[a-z]+\.[a-z]+@spc\.edu\.pe$', v.lower()):
            raise ValueError("Formato de correo inválido")
        return v.lower().strip()

# Endpoints principales
@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/docs")

@app.get("/health")
async def health_check():
    services = {
        "firebase": bool(db),
        "gemini": bool(modelo),
        "status": "running",
        "timestamp": datetime.now().isoformat()
    }
    return JSONResponse(content=services)

@app.post("/register", response_model=Dict)
async def register(user: UserRequest):
    try:
        email = UserRequest.validate_email(user.email)
        password = user.password.strip()
        
        if len(password) != 8:
            raise HTTPException(400, "La contraseña debe tener 8 caracteres")
        
        if not db:
            raise HTTPException(500, "Error de base de datos")
        
        users_ref = db.collection("students")
        query = users_ref.where("email", "==", email).limit(1)
        docs = await query.get()
        
        if docs:
            raise HTTPException(400, "El correo ya está registrado")
        
        await users_ref.add({
            "email": email,
            "password": password,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        
        return {"success": True, "email": email}
        
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Register error: {str(e)}")
        raise HTTPException(500, "Error en el servidor")

@app.post("/login", response_model=Dict)
async def login(user: UserRequest):
    try:
        email = UserRequest.validate_email(user.email)
        password = user.password.strip()
        
        if not db:
            raise HTTPException(500, "Error de base de datos")
        
        users_ref = db.collection("students")
        query = users_ref.where("email", "==", email) \
                        .where("password", "==", password) \
                        .limit(1)
        docs = await query.get()
        
        if not docs:
            raise HTTPException(401, "Credenciales incorrectas")
        
        return {"success": True, "email": email}
        
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        raise HTTPException(500, "Error en el servidor")

@app.post("/welcome", response_model=Dict)
async def welcome(user: UserRequest):
    try:
        email = UserRequest.validate_email(user.email)
        name = email.split('@')[0].replace('.', ' ').title()
        message = f"¡Hola {name}! 👋 Soy Coprodelito, tu asistente emocional. ¿Cómo te sientes hoy?"
        
        async with state.lock:
            state.reset()
            state.student_email = email
            state.history = [{"role": "assistant", "content": message}]
        
        return {"response": message}
        
    except Exception as e:
        logger.error(f"Welcome error: {str(e)}")
        raise HTTPException(500, "Error al generar bienvenida")

@app.post("/chat", response_model=Dict)
async def chat(chat_data: ChatRequest):
    try:
        if not modelo:
            raise HTTPException(503, "Servicio de IA no disponible")
        
        message = ChatRequest.validate_message(chat_data.message)
        
        async with state.lock:
            if not state.student_email:
                raise HTTPException(400, "Sesión no iniciada")
            
            # Generar respuesta
            response = await generate_response(message)
            return {"response": response}
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Chat error: {str(e)}")
        raise HTTPException(500, "Error procesando mensaje")

# Funciones auxiliares
async def generate_response(message: str) -> str:
    """Genera una respuesta usando Gemini con manejo de estado"""
    state.history.append({"role": "user", "content": message})
    
    if not state.first_message:
        state.first_message = message
    
    try:
        # Construir contexto
        context = "\n".join(
            f"{msg['role']}: {msg['content']}" 
            for msg in state.history[-5:]
        )
        
        # Generar respuesta
        prompt = f"""Eres Coprodelito, un asistente emocional. Contexto:
{context}

Nuevo mensaje: "{message}"

Responde de forma empática y natural."""
        
        response = await modelo.generate_content_async(
            prompt,
            timeout=30,
            safety_settings={
                "HARM_CATEGORY_HARASSMENT": "BLOCK_NONE",
                "HARM_CATEGORY_HATE_SPEECH": "BLOCK_NONE",
                "HARM_CATEGORY_SEXUALLY_EXPLICIT": "BLOCK_NONE",
                "HARM_CATEGORY_DANGEROUS_CONTENT": "BLOCK_NONE"
            }
        )
        
        text = response.text.strip()
        
        # Procesamiento adicional
        text = process_response(text, message)
        state.history.append({"role": "assistant", "content": text})
        
        return text
        
    except Exception as e:
        logger.error(f"Error generando respuesta: {str(e)}")
        return "¡Vaya! Algo salió mal. Por favor inténtalo de nuevo."

def process_response(text: str, original_msg: str) -> str:
    """Procesa la respuesta para añadir emociones y formato"""
    if not text:
        return "No pude generar una respuesta. ¿Puedes intentarlo de nuevo?"
    
    # Detección de emociones
    if is_topic_change() and "emoción detectada" not in text.lower():
        emotion = detect_emotion(original_msg)
        if emotion:
            text = f"Emoción detectada: {emotion} 😊\n{text}"
    
    # Formateo de recomendaciones
    if needs_recommendations(original_msg) and "🔹" not in text:
        lines = [line.strip() for line in text.split('\n') if line.strip()]
        text = "\n".join(f"🔹 {line}" for line in lines[:3])
    
    return text

def is_topic_change() -> bool:
    """Determina si hubo un cambio de tema"""
    if len(state.history) < 2:
        return True
        
    last_messages = [
        msg['content'].lower() 
        for msg in state.history[-3:] 
        if msg['role'] == 'user'
    ]
    connectors = ["y", "además", "también", "pero", "aunque", "luego"]
    return not any(conn in ' '.join(last_messages) for conn in connectors)

def needs_recommendations(text: str) -> bool:
    """Determina si el usuario pide recomendaciones"""
    keywords = ["consejo", "recomendación", "qué hago", "ayuda", "sugerencia"]
    return any(key in text.lower() for key in keywords)

def detect_emotion(text: str) -> Optional[str]:
    """Intenta detectar la emoción principal en el texto"""
    try:
        if not modelo:
            return None
            
        response = modelo.generate_content(
            f"Identifica la emoción principal en este texto (responde con una sola palabra): {text}"
        )
        return response.text.strip().capitalize()
    except:
        return None

# Manejo de errores global
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Error no manejado: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno del servidor"}
    )

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
        timeout_keep_alive=60
    )

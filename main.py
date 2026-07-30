from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from routes.upload import router as upload_router
from routes.interview import router as interview_router

app = FastAPI(
    title="Interview Expert API",
    description="Real-time AI interview assistant powered by Gemini Live API",
    version="1.0.0",
)

# CORS — allow the React frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(upload_router, prefix="/api")
app.include_router(interview_router, prefix="/api")


@app.get("/api/health")
async def health_check():
    return {"status": "ok", "service": "Interview Expert API"}

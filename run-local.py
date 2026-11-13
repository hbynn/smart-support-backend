import os
from pathlib import Path
root = Path(__file__).parent
os.environ.update(REDIS_URL='redis://localhost:6379/0', CHROMA_HOST='localhost', CHROMA_PORT='8001', CHROMA_PERSIST_DIRECTORY=str(root/'data'/'chroma-win'), ECHOMIND_ENV='development')
import uvicorn
uvicorn.run('api.main:app', host='127.0.0.1', port=8000)

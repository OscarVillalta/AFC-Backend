from . import startup
from app import create_app
from database import models
app = create_app()
app.run(host="0.0.0.0", port=5000, debug=False)
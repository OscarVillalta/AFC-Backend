import os, requests
from flask import Blueprint, request, jsonify
from marshmallow import ValidationError
from app.api.Schemas.qb_schema import QBJobResultSchema, QBJobSchema
from app.config import Config
from app.api.tokens import permission_required


QB_AGENT_URL = QB_AGENT_URL = os.getenv("QB_AGENT_URL", "http://127.0.0.1:5055")

def qb_agent_post(job: dict, timeout=60) -> dict:
    headers = {"X-API-Key": Config.QB_API_KEY}
    r = requests.post(f"{Config.QB_AGENT_URL}/jobs", json=job, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()

qb_bp = Blueprint("qb", __name__)

job_schema = QBJobSchema()
job_result_schema = QBJobResultSchema()


@qb_bp.post("/qb/job")
@permission_required("qb:pull_orders", "qb:sync_catalog")
def run_qb_job():
    try:
        job = job_schema.load(request.get_json(force=True))
    except ValidationError as err:
        return jsonify({
            "success": False,
            "error": "Invalid job payload",
            "details": err.messages
        }), 400

    headers = {"X-API-Key": Config.QB_API_KEY}
    r = requests.post(
        f"{Config.QB_AGENT_URL}/jobs",
        json=job,
        headers=headers,
        timeout=60
    )
    r.raise_for_status()

    result = job_result_schema.load(r.json())
    return jsonify(result)
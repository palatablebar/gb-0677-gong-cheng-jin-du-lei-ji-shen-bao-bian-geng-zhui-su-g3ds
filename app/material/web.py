"""甲供材工作台页面路由（独立于七阶段计量流水线）。"""
from flask import render_template

from app.config import TITLE, SHORT, MODULES
from app.material import store
from app.material.handler import _sample_bytes
from app.material.intake import parse_payload
from app.material.rules import adjudicate


def register_material_pages(app):
    @app.get("/materials")
    def material_workbench():
        events = store.load_events()
        if events:
            result = adjudicate(events, closed_periods=store.closed_periods())
            sample_mode = False
        else:
            parsed, _ = parse_payload(_sample_bytes())
            result = adjudicate(parsed)
            sample_mode = True
        return render_template(
            "materials.html",
            title=TITLE, short=SHORT, modules=MODULES, index=len(MODULES),
            result=result, certificates=store.list_certificates(),
            sample_mode=sample_mode)

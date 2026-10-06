"""可公开核验的现场抽签系统 —— Flask 应用工厂。"""
import os

from flask import Flask

from . import db


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(
        __name__,
        instance_relative_config=False,
        static_folder=os.path.join(os.path.dirname(os.path.dirname(__file__)), "static"),
    )
    app.config["DB_PATH"] = os.environ.get(
        "LOTTERY_DB", os.path.join(os.getcwd(), "lottery.db")
    )
    app.config["JSON_AS_ASCII"] = False
    if test_config:
        app.config.update(test_config)

    os.makedirs(os.path.dirname(os.path.abspath(app.config["DB_PATH"])), exist_ok=True)

    db.init_app(app)

    from .routes import bp

    app.register_blueprint(bp)
    return app

from . import (
    api,
    auth,
    editor,
    main,
    frontend_api,
    api_auth,
    notifications_api,
    notification_engine_api,
    startup,
)


# Register Blueprints
def register_blueprints(app):
    app.register_blueprint(main.bp)
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(editor.bp)
    app.register_blueprint(frontend_api.bp)
    app.register_blueprint(api_auth.bp)
    app.register_blueprint(notifications_api.bp)
    app.register_blueprint(notification_engine_api.bp)
    return True


__all__ = [
    "api",
    "auth",
    "editor",
    "main",
    "frontend_api",
    "api_auth",
    "notifications_api",
    "notification_engine_api",
    "startup",
]

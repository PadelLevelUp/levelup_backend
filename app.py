import os

from dotenv import load_dotenv

import padel_app as app

if os.getenv("FLASK_ENV") != "production":
    load_dotenv(".env.local.dev")
    if os.path.exists(".secrets.env"):
        load_dotenv(".secrets.env")

run_app = app.create_app()

if __name__ == "__main__":
    # SSE connections stay open for long periods, so the built-in server must
    # allow concurrent requests in this runtime mode.
    run_app.run(host="0.0.0.0", port=80, threaded=True)
"""

Se for preciso correr sem 'flask run' talvez seja necessario ter isto:
run_app.run(debug=True)

(Talvez dentro dum if __name__="__main__")

"""

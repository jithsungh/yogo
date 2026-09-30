"""Pydantic models: the contract between the service layer and any client.

Services accept these (or plain dicts validated into them) and return the
*Out models - never ORM rows. That is what lets Streamlit today and a real
frontend over the FastAPI layer (app/api) tomorrow share one backend.
"""

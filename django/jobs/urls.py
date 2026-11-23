# jobs/urls.py
from django.urls import path
from jobs.views import job_detail, job_logs

urlpatterns = [
    path('<int:job_id>/', job_detail, name='job_detail'),
    path('<int:job_id>/logs/', job_logs, name='job_logs'),
]

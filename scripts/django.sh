#!/usr/bin/env bash
# scripts/django.sh

run_migrations() {

    log_info "检查 Django Models"

    if [[ -n "$PRIORITY_APP" ]]; then
        MANAGE makemigrations "$PRIORITY_APP" --noinput
    fi

    for app in $APPS_LIST; do

        [[ "$app" == "$PRIORITY_APP" ]] && continue

        MANAGE makemigrations "$app" --noinput
    done

    MANAGE makemigrations --noinput

    log_info "执行 migrate"

    MANAGE migrate --noinput
}

ensure_superuser() {

    log_info "检查超级用户"

    MANAGE shell -c "
import os

from django.contrib.auth import get_user_model

User = get_user_model()

username = os.environ.get('DJANGO_ADMIN_USER', 'admin')
email = os.environ.get('DJANGO_ADMIN_EMAIL', 'admin@example.com')
password = os.environ.get('DJANGO_ADMIN_PASSWORD', 'ChangeMe_Admin_2026')

user, created = User.objects.get_or_create(
    username=username,
    defaults={
        'email': email,
        'is_superuser': True,
        'is_staff': True,
    }
)

if created:
    user.set_password(password)
    user.save()
    print('Superuser created')
else:
    print('Superuser already exists')
"
}
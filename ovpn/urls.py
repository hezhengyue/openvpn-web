#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ovpn/urls.py —— 原 Flask app.py 全部路由的 Django 映射"""

from django.urls import path

from . import auth, views
from .bridge import method_split

urlpatterns = [
    # 会话 / 鉴权
    path("api/session", auth.api_session, name="api_session"),
    path("api/login", auth.api_login, name="api_login"),
    path("api/logout", auth.api_logout, name="api_logout"),
    path("api/password", auth.api_password, name="api_password"),
    path("api/admin-user", auth.api_admin_user, name="api_admin_user"),
    path("api/security/allow", method_split(GET=views.api_allow_get, POST=views.api_allow_set), name="allow"),

    # 用户自助（拨 VPN 后按来源 IP 认人，免管理登录）
    path("self", views.self_page, name="self_page"),
    path("api/self/whoami", auth.api_self_whoami, name="self_whoami"),
    path("api/self/password", auth.api_self_password, name="self_password"),

    # 长任务
    path("api/task/<str:tid>", views.api_task, name="api_task"),
    path("api/task/<str:tid>/log", views.api_task_log, name="api_task_log"),

    # 概览
    path("api/state", views.api_state, name="api_state"),

    # 安装向导
    path("api/wizard/check", views.wizard_check, name="wizard_check"),
    path("api/wizard/params", views.wizard_params, name="wizard_params"),
    path("api/wizard/binary", views.wizard_binary, name="wizard_binary"),
    path("api/wizard/execute", views.wizard_execute, name="wizard_execute"),
    path("api/wizard/steps", views.wizard_steps, name="wizard_steps"),

    # 账号
    path("api/users", method_split(GET=views.users_get, POST=views.users_add), name="users"),
    path("api/users/next-ip", views.users_next_ip, name="users_next_ip"),
    path("api/users/export", views.users_export, name="users_export"),
    path("api/users/import", views.users_import, name="users_import"),
    path("api/users/<str:name>", method_split(POST=views.users_update, DELETE=views.users_delete), name="users_item"),

    # ACL / 资产 / 授权
    path("api/acl", views.acl_get, name="acl_get"),
    path("api/network/nat", views.api_nat_mode, name="nat_mode"),
    path("api/peers", views.peers_add, name="peers_add"),
    path("api/peers/<int:pid>", views.peers_del, name="peers_del"),
    path("api/asset-groups", views.asset_group_add, name="asset_group_add"),
    path("api/asset-groups/<int:gid>", method_split(POST=views.asset_group_update, DELETE=views.asset_group_del), name="asset_group_item"),
    path("api/asset-groups/<int:gid>/items", views.asset_item_add, name="asset_item_add"),
    path("api/asset-items/<int:iid>", method_split(POST=views.asset_item_update, DELETE=views.asset_item_del), name="asset_item"),
    path("api/user-groups", views.user_group_add, name="user_group_add"),
    path("api/user-groups/<int:gid>", method_split(POST=views.user_group_update, DELETE=views.user_group_del), name="user_group_item"),
    path("api/grants", views.grants_add, name="grants_add"),
    path("api/grants/<int:gid>", views.grants_del, name="grants_del"),
    path("api/acl/mode", views.acl_mode, name="acl_mode"),

    # 服务端设置
    path("api/settings", method_split(GET=views.settings_get, POST=views.settings_save), name="settings"),
    path("api/settings/preview", views.settings_preview, name="settings_preview"),
    path("api/settings/public-ip", views.settings_public_ip, name="settings_public_ip"),
    path("api/settings/capacity", views.settings_capacity, name="settings_capacity"),
    path("api/settings/rebuild", views.settings_rebuild, name="settings_rebuild"),
    path("api/settings/reinit-ca", views.settings_reinit_ca, name="settings_reinit_ca"),
    path("api/settings/restore-default", views.settings_restore_default, name="settings_restore_default"),

    # PKI / 客户端配置
    path("api/pki", views.api_pki, name="api_pki"),
    path("api/pki/<str:name>", views.api_pki_file, name="api_pki_file"),
    path("api/client.ovpn", views.client_ovpn, name="client_ovpn"),

    # 版本与更新
    path("api/version", views.version_get, name="version_get"),
    path("api/update/binary", views.update_binary, name="update_binary"),
    path("api/update/build-env", views.update_build_env, name="update_build_env"),
    path("api/update/source", views.update_source, name="update_source"),
    path("api/update/web", views.update_web, name="update_web"),

    # 备份
    path("api/backups", method_split(GET=views.backups_get, POST=views.backups_create), name="backups"),
    path("api/backups/restore", views.backups_restore, name="backups_restore"),
    path("api/user-backups", views.user_backups_list, name="user_backups_list"),
    path("api/user-backups/download", views.user_backup_download, name="user_backup_download"),
    path("api/user-backups/restore", views.user_backup_restore, name="user_backup_restore"),
    path("api/full-backup/list", views.full_backup_list, name="full_backup_list"),
    path("api/full-backup/create", views.full_backup_create, name="full_backup_create"),
    path("api/full-backup/download/<str:name>", views.full_backup_download, name="full_backup_download"),
    path("api/full-backup/upload", views.full_backup_upload, name="full_backup_upload"),
    path("api/full-backup/restore", views.full_backup_restore, name="full_backup_restore"),
    path("api/full-backup/delete", views.full_backup_delete, name="full_backup_delete"),

    # 在线 / 日志 / 自检 / 路由 / 服务
    path("api/online", views.api_online, name="api_online"),
    path("api/logs", views.api_logs, name="api_logs"),
    path("api/selfcheck", views.api_selfcheck, name="api_selfcheck"),
    path("api/route-guide", views.api_route_guide, name="api_route_guide"),
    path("api/service/restart", views.api_restart, name="api_restart"),

    # 前端
    path("", views.index, name="index"),
    path("favicon.ico", views.favicon, name="favicon"),
]

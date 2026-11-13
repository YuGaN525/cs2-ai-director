"""Stable account-security errors; never trust a remote free-text message as a code."""
import re

ERROR_STATUS = {
    'ACCOUNT_SECURITY_FROZEN': 403,
    'DEVICE_SECURITY_BLOCKED': 403,
    'EMAIL_REAUTH_REQUIRED': 403,
    'SESSION_REVOKED': 401,
}
MESSAGES = {
    'ACCOUNT_SECURITY_FROZEN': '账号已安全冻结，自动连接已暂停。请联系超级管理员解冻；重新授权不能解除冻结。',
    'DEVICE_SECURITY_BLOCKED': '当前设备凭据已被安全封禁，自动连接已暂停。请联系超级管理员处理；重新授权不能解除封禁。',
    'EMAIL_REAUTH_REQUIRED': '需要邮箱再验证。请点击登录，在网站完成邮箱验证与设备授权。',
    'SESSION_REVOKED': '当前会话已撤销，自动连接已暂停。请点击登录重新授权。',
}


def response_code(body, status):
    if isinstance(body, dict):
        for key in ('code', 'error'):
            value = body.get(key)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', value):
                return value
    return 'http_' + str(status)


def is_security_error(code, status):
    return isinstance(code, str) and code in ERROR_STATUS and ERROR_STATUS[code] == status


def notify_account(account, error, identity):
    """Only an error for the same in-flight identity can invalidate its session."""
    code, status = getattr(error, 'code', ''), getattr(error, 'status', 0)
    if is_security_error(code, status):
        handler = getattr(account, 'handle_security_error', None)
        if handler: handler(code, status, identity)
        return MESSAGES[code]
    return ''

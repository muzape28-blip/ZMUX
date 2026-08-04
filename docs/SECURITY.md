# ZMUX Security

## Threat Model

### 1. WebView Origin Isolation
**Risk:** Third-party content injected into WebView.
**Mitigation:**
- Content-Security-Policy restricts to `'self'` only
- No `frame-ancestors` (no embedding)
- Loopback-only server (127.0.0.1)
- Auth token on all sensitive endpoints

### 2. Auth Token
**Risk:** Unauthorized API access.
**Mitigation:**
- 128-bit random hex token per installation
- Constant-time comparison (`hmac.compare_digest`)
- Embedded in HTML template (WebView-only access)
- Required header `X-ZMUX-Token` on sensitive routes

### 3. Command Injection
**Risk:** Malicious command execution.
**Mitigation:**
- Built-in commands (cd, pwd) handled without shell
- shell=True used only for general commands (documented)
- User is explicitly in a terminal environment
- Path traversal blocked for built-in cd

### 4. Package Supply Chain
**Risk:** Malicious wheel package.
**Mitigation:**
- HTTPS-only for metadata and downloads
- SHA-256 mandatory and verified
- ZIP path traversal rejection
- Duplicate member rejection
- Size limits enforced
- Smoke-import before commit
- Transactional install with rollback
- File ownership tracking

### 5. File System Access
**Risk:** Unauthorized file access.
**Mitigation:**
- App-private storage for all ZMUX directories
- Built-in cd restricts to HOME_DIR
- Shell commands can access OS-permitted areas (documented risk)
- Uninstall only removes owned files

### 6. Key Storage
**Risk:** API key theft from device.
**Mitigation:**
- Android Keystore preferred (hardware-backed)
- Fallback: PBKDF2-derived encryption with random master key
- Encrypt-then-MAC with HMAC-SHA256
- Constant-time tag verification
- 0600 permissions on key files

### 7. TLS/SSL
**Risk:** MITM attacks on package downloads.
**Mitigation:**
- certifi CA bundle shipped in APK
- No `--trusted-host` or TLS bypass
- No HTTP fallback for package operations
- Verified SSL context shared across modules

## Security Headers

```
Content-Security-Policy: default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval'; ...
X-Content-Type-Options: nosniff
Referrer-Policy: no-referrer
```

## Permissions

ZMUX requests minimum permissions:
- `INTERNET` — for package downloads
- No `READ_EXTERNAL_STORAGE` or `WRITE_EXTERNAL_STORAGE`

## Limitations

- Shell commands (`/system/bin/sh`) can access areas permitted by Android OS
- On rooted devices, app-private storage may be readable
- WebView cannot fully isolate from the host process

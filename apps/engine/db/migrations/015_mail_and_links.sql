-- E10.3–E10.6: links de decisão no e-mail (uso único, 72 h), lotes de aprovação e resultado "incerto" no envio.

-- Cada link guarda só o necessário para o validar e gastar uma vez; o token vai assinado (HMAC) no e-mail e não
-- se guarda. Um clique (POST) gasta o link e os irmãos do mesmo job.
CREATE TABLE IF NOT EXISTS approval_links (
    id         BIGSERIAL PRIMARY KEY,
    job_id     BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    action     TEXT        NOT NULL CHECK (action IN ('approve', 'reject')),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    used_by    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approval_links_job ON approval_links (job_id);

-- Resultado ambíguo de um envio (timeout, ligação cortada): `unknown`, nunca repetido sozinho (SEND_UNKNOWN).
ALTER TABLE daily_digest DROP CONSTRAINT IF EXISTS daily_digest_status_check;
ALTER TABLE daily_digest
    ADD CONSTRAINT daily_digest_status_check CHECK (status IN ('pending', 'sent', 'failed', 'unknown'));

-- Lotes de pedidos de aprovação (06:30, 10:00, 13:00, 16:00): um por dia, hora e destinatário.
CREATE TABLE IF NOT EXISTS approval_batches (
    local_date  DATE        NOT NULL,
    slot        TEXT        NOT NULL,
    recipient   TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'sent', 'failed', 'unknown')),
    jobs        INTEGER     NOT NULL DEFAULT 0,
    sent_at_utc TIMESTAMPTZ,
    error       TEXT,
    PRIMARY KEY (local_date, slot, recipient)
);

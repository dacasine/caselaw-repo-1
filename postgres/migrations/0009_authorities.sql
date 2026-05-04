-- Swiss judicial authorities directory + judges roster.

CREATE TABLE IF NOT EXISTS authorities (
    id              SERIAL PRIMARY KEY,
    court_code      TEXT UNIQUE,                -- maps to decisions.court (e.g. "bger", "zh_obergericht")
    name_fr         TEXT NOT NULL,
    name_de         TEXT,
    name_it         TEXT,
    level           INTEGER,                    -- 1=admin, 2=1ère instance, 3=cantonal suprême, 4=fédéral spécialisé, 5=TF
    canton          TEXT,                       -- 2-letter code or 'CH' for federal
    parent_id       INTEGER REFERENCES authorities(id),
    address         TEXT,
    postal_code     TEXT,
    city            TEXT,
    phone           TEXT,
    fax             TEXT,
    email           TEXT,
    email_secure    TEXT,                       -- IncaMail / Privasphere / SETYPE
    website         TEXT,
    platform        TEXT,                       -- 'decwork', 'tribuna', 'weblaw', 'findinfo', etc.
    jurisdiction    TEXT[],                     -- ['civil','penal','admin','social','fiscal']
    chambers        JSONB,                      -- [{name_fr, name_de, jurisdiction}]
    notes           TEXT,
    source          TEXT DEFAULT 'manual',      -- 'manual' | 'scraped' | 'annuaire.admin.ch'
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_canton ON authorities (canton);
CREATE INDEX IF NOT EXISTS idx_auth_level ON authorities (level);
CREATE INDEX IF NOT EXISTS idx_auth_court_code ON authorities (court_code);

CREATE TABLE IF NOT EXISTS judges (
    id              SERIAL PRIMARY KEY,
    authority_id    INTEGER NOT NULL REFERENCES authorities(id) ON DELETE CASCADE,
    last_name       TEXT NOT NULL,
    first_name      TEXT,
    title           TEXT,                       -- 'Dr.', 'Prof. Dr.', 'lic. iur.', 'MLaw'
    function        TEXT NOT NULL,              -- 'président', 'vice-président', 'juge', 'juge suppléant', 'greffier', 'greffière'
    chamber         TEXT,                       -- chamber/section name if applicable
    language        TEXT,                       -- principal language de/fr/it
    gender          TEXT,                       -- 'm'/'f' (for correct salutation)
    start_date      DATE,
    end_date        DATE,                       -- NULL = still active
    party           TEXT,                       -- political party (public info for federal judges)
    source          TEXT DEFAULT 'manual',      -- 'manual' | 'scraped' | 'extracted_from_decisions'
    extracted_count INTEGER DEFAULT 0,          -- how many decisions mention this judge
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_judges_authority ON judges (authority_id);
CREATE INDEX IF NOT EXISTS idx_judges_name ON judges (last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_judges_function ON judges (function);
CREATE INDEX IF NOT EXISTS idx_judges_active ON judges (authority_id) WHERE end_date IS NULL;

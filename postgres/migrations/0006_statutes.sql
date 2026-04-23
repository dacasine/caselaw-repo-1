-- Federal + cantonal law articles (mirrors statutes.db + cantonal_laws.db)

-- Federal laws (Fedlex)
CREATE TABLE IF NOT EXISTS laws_federal (
    sr_number           TEXT PRIMARY KEY,
    title_de            TEXT,
    title_fr            TEXT,
    title_it            TEXT,
    abbr_de             TEXT,
    abbr_fr             TEXT,
    abbr_it             TEXT,
    consolidation_date  TEXT,
    work_uri            TEXT
);

CREATE INDEX IF NOT EXISTS idx_lawsfed_abbr_de ON laws_federal (abbr_de);
CREATE INDEX IF NOT EXISTS idx_lawsfed_abbr_fr ON laws_federal (abbr_fr);
CREATE INDEX IF NOT EXISTS idx_lawsfed_abbr_it ON laws_federal (abbr_it);

-- Federal law articles
CREATE TABLE IF NOT EXISTS articles_federal (
    id          BIGSERIAL PRIMARY KEY,
    sr_number   TEXT NOT NULL,
    lang        TEXT NOT NULL,
    article_num TEXT NOT NULL,
    heading     TEXT,
    footnote    TEXT,
    text        TEXT NOT NULL,
    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', immutable_unaccent(COALESCE(heading, ''))), 'A') ||
        setweight(to_tsvector('simple', immutable_unaccent(COALESCE(text,    ''))), 'B')
    ) STORED,
    UNIQUE (sr_number, lang, article_num),
    FOREIGN KEY (sr_number) REFERENCES laws_federal(sr_number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artfed_sr_article ON articles_federal (sr_number, article_num);
CREATE INDEX IF NOT EXISTS idx_artfed_fts        ON articles_federal USING GIN (fts);


-- Cantonal laws (LexFind + direct portals)
CREATE TABLE IF NOT EXISTS laws_cantonal (
    lexfind_id           BIGINT,
    language             TEXT NOT NULL,
    canton               TEXT NOT NULL,
    sr_number            TEXT,
    title                TEXT NOT NULL,
    category             TEXT,
    is_active            BOOLEAN NOT NULL DEFAULT TRUE,
    original_url         TEXT,
    version_active_since DATE,
    text_length          INTEGER,
    article_count        INTEGER,
    text_source          TEXT,
    full_text            TEXT,
    fetched_at           TIMESTAMPTZ,
    PRIMARY KEY (lexfind_id, language)
);

CREATE INDEX IF NOT EXISTS idx_lawscant_canton      ON laws_cantonal (canton);
CREATE INDEX IF NOT EXISTS idx_lawscant_canton_lang ON laws_cantonal (canton, language);
CREATE INDEX IF NOT EXISTS idx_lawscant_sr          ON laws_cantonal (sr_number);


CREATE TABLE IF NOT EXISTS articles_cantonal (
    id          BIGSERIAL PRIMARY KEY,
    lexfind_id  BIGINT NOT NULL,
    language    TEXT   NOT NULL,
    canton      TEXT   NOT NULL,
    seq         INTEGER NOT NULL,
    article_num TEXT,
    heading     TEXT,
    text        TEXT   NOT NULL,
    fts tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', immutable_unaccent(COALESCE(heading, ''))), 'A') ||
        setweight(to_tsvector('simple', immutable_unaccent(COALESCE(text,    ''))), 'B')
    ) STORED,
    FOREIGN KEY (lexfind_id, language) REFERENCES laws_cantonal(lexfind_id, language) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artcant_law      ON articles_cantonal (lexfind_id, language);
CREATE INDEX IF NOT EXISTS idx_artcant_canton   ON articles_cantonal (canton);
CREATE INDEX IF NOT EXISTS idx_artcant_fts      ON articles_cantonal USING GIN (fts);

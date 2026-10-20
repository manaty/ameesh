-- #2845: cursor and mailbox delivery are committed together.
CREATE TABLE github_watch_cursors (
    project text NOT NULL,
    repo text NOT NULL,
    scanned_through double precision NOT NULL,
    PRIMARY KEY (project, repo)
);
CREATE TABLE github_watch_events (
    project text NOT NULL,
    repo text NOT NULL,
    kind text NOT NULL,
    object_id bigint NOT NULL,
    digest text NOT NULL,
    PRIMARY KEY (project, repo, kind, object_id)
);

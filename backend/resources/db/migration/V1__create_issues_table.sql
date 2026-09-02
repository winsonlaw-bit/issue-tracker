-- Issues table — stores all shipment issue records.
-- OceanBase uses MySQL syntax: BIGINT AUTO_INCREMENT, no SERIAL, no RETURNING.

CREATE TABLE issues (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    reference   VARCHAR(50)  NOT NULL,
    lane        VARCHAR(100) NOT NULL,
    issue_type  VARCHAR(100) NOT NULL,
    status      VARCHAR(10)  NOT NULL DEFAULT 'open',
    owner       VARCHAR(100) NOT NULL,
    notes       TEXT,
    created_at  TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

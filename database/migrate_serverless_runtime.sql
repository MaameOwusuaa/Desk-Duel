-- Run once on an existing desk_duel database before deploying the polling runtime.
USE desk_duel;

ALTER TABLE games
  ADD COLUMN game_phase ENUM('lobby','get_ready','question','reveal','finished') NOT NULL DEFAULT 'lobby',
  ADD COLUMN current_position INT NOT NULL DEFAULT 0,
  ADD COLUMN phase_started_at DATETIME(6) NULL,
  ADD COLUMN state_version BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN phase_version BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN last_event_type VARCHAR(24) NULL,
  ADD COLUMN last_event_player_id INT NULL,
  ADD COLUMN last_event_option CHAR(1) NULL,
  ADD INDEX idx_games_phase (game_phase);

ALTER TABLE players
  ADD COLUMN correct_ms_total BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN rank INT NULL,
  ADD COLUMN previous_rank INT NULL,
  ADD COLUMN is_online TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN last_seen_at DATETIME(6) NULL,
  ADD INDEX idx_players_presence (game_id, is_online, last_seen_at);

-- Prior in-memory games cannot be resumed. Close active games but preserve their history.
UPDATE games SET status = 'finished', game_phase = 'finished', ended_at = COALESCE(ended_at, NOW()),
  phase_started_at = NOW(6), state_version = 1, phase_version = 1 WHERE status = 'in_progress';
UPDATE games SET game_phase = 'finished', phase_version = state_version WHERE status = 'finished';
UPDATE games SET game_phase = 'lobby', current_position = 0, phase_started_at = NOW(6),
  state_version = 1, phase_version = 1 WHERE status = 'lobby';

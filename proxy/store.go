package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"time"

	"github.com/gofrs/flock"
	_ "modernc.org/sqlite"
)

const schemaVersion = 1

type profile struct {
	Name           string    `json:"name"`
	ID             string    `json:"id"`
	Host           string    `json:"host"`
	User           string    `json:"user,omitempty"`
	SSHPort        int       `json:"ssh_port,omitempty"`
	SSHConfig      string    `json:"ssh_config,omitempty"`
	Target         string    `json:"route_target,omitempty"`
	Port           int       `json:"port,omitempty"`
	Owner          string    `json:"-"`
	PID            int       `json:"pid,omitempty"`
	Phase          string    `json:"phase"`
	Desired        bool      `json:"desired"`
	Deadline       time.Time `json:"deadline"`
	RetryAt        time.Time `json:"retry_at"`
	Failures       int       `json:"ssh_failures"`
	WorkerFailures int       `json:"worker_failures"`
	Generation     uint64    `json:"generation"`
	Problem        string    `json:"problem,omitempty"`
	CleanupFailed  bool      `json:"cleanup_failed,omitempty"`
	StopSeq        uint64    `json:"stop_sequence"`
	Rev            int64     `json:"-"`
}

type diskRecord struct {
	Profile profile `json:"profile"`
	Owner   string  `json:"owner"`
}

type store struct {
	db  *sql.DB
	dir string
}

var errMissing = errors.New("proxy profile not found")

func openStore(ctx context.Context, dir string, writable bool) (*store, error) {
	path := filepath.Join(dir, "proxy-v1.sqlite3")
	if writable {
		if err := os.MkdirAll(dir, 0700); err != nil {
			return nil, err
		}
		info, err := os.Stat(dir)
		if err != nil {
			return nil, err
		}
		if runtime.GOOS != "windows" && info.Mode().Perm()&0022 != 0 {
			return nil, errors.New("proxy state directory must be writable only by its owner")
		}
		f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err == nil {
			err = f.Close()
		}
		if err != nil && !errors.Is(err, os.ErrExist) {
			return nil, err
		}
	}
	info, err := os.Lstat(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, errMissing
		}
		return nil, err
	}
	if !info.Mode().IsRegular() {
		return nil, errors.New("proxy state must be a regular SQLite file")
	}
	mode := "ro"
	if writable {
		mode = "rw"
	}
	u := url.URL{Scheme: "file", Path: filepath.ToSlash(path)}
	if filepath.VolumeName(path) != "" {
		u.Path = "/" + u.Path
	}
	q := u.Query()
	q.Set("mode", mode)
	q.Set("_pragma", "busy_timeout(2500)")
	u.RawQuery = q.Encode()
	db, err := sql.Open("sqlite", u.String())
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if writable {
		_, err = db.ExecContext(ctx, "CREATE TABLE IF NOT EXISTS profiles (name TEXT PRIMARY KEY, rev INTEGER NOT NULL, data TEXT NOT NULL)")
		if err != nil {
			db.Close()
			return nil, err
		}
	}
	return &store{db: db, dir: dir}, nil
}

func (s *store) lock(id string) *flock.Flock {
	return flock.New(filepath.Join(s.dir, id+".lock"), flock.SetPermissions(0600))
}

func token() (string, error) {
	var b [16]byte
	_, err := rand.Read(b[:])
	return hex.EncodeToString(b[:]), err
}

type scanner interface{ Scan(...any) error }

func decode(row scanner) (profile, error) {
	var rev int64
	var body []byte
	if err := row.Scan(&rev, &body); err != nil {
		if errors.Is(err, sql.ErrNoRows) {
			return profile{}, errMissing
		}
		return profile{}, err
	}
	var data diskRecord
	if err := json.Unmarshal(body, &data); err != nil {
		return profile{}, fmt.Errorf("read proxy record: %w", err)
	}
	decodedID, err := hex.DecodeString(data.Profile.ID)
	if err != nil || len(decodedID) != 16 {
		return profile{}, errors.New("invalid proxy profile identity")
	}
	data.Profile.Rev = rev
	data.Profile.Owner = data.Owner
	return data.Profile, nil
}

func (s *store) get(ctx context.Context, name string) (profile, error) {
	return decode(s.db.QueryRowContext(ctx, "SELECT rev, data FROM profiles WHERE name = ?", name))
}

type profileReader interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func (s *store) list(ctx context.Context) ([]profile, error) {
	return listProfiles(ctx, s.db)
}

func listProfiles(ctx context.Context, db profileReader) ([]profile, error) {
	items := make([]profile, 0)
	rows, err := db.QueryContext(ctx, "SELECT rev, data FROM profiles ORDER BY name")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		p, err := decode(rows)
		if err != nil {
			return nil, err
		}
		items = append(items, p)
	}
	return items, rows.Err()
}

type profileWriter interface {
	ExecContext(context.Context, string, ...any) (sql.Result, error)
}

func (s *store) add(ctx context.Context, p profile) error {
	return insertProfile(ctx, s.db, p)
}

func insertProfile(ctx context.Context, db profileWriter, p profile) error {
	b, err := json.Marshal(diskRecord{p, p.Owner})
	if err != nil {
		return err
	}
	result, err := db.ExecContext(ctx, "INSERT INTO profiles VALUES (?, 1, ?) ON CONFLICT DO NOTHING", p.Name, b)
	if err != nil {
		return err
	}
	n, err := result.RowsAffected()
	if err == nil && n == 0 {
		return fmt.Errorf("proxy profile %q already exists", p.Name)
	}
	return err
}

func (s *store) change(ctx context.Context, p profile) (bool, error) {
	b, err := json.Marshal(diskRecord{p, p.Owner})
	if err != nil {
		return false, err
	}
	r, err := s.db.ExecContext(ctx, "UPDATE profiles SET rev = rev + 1, data = ? WHERE name = ? AND rev = ? AND json_extract(data, '$.profile.id') = ?", b, p.Name, p.Rev, p.ID)
	if err != nil {
		return false, err
	}
	n, err := r.RowsAffected()
	return n == 1, err
}

func (s *store) remove(ctx context.Context, p profile) (bool, error) {
	r, err := s.db.ExecContext(ctx, "DELETE FROM profiles WHERE name = ? AND rev = ? AND json_extract(data, '$.profile.id') = ?", p.Name, p.Rev, p.ID)
	if err != nil {
		return false, err
	}
	n, err := r.RowsAffected()
	return n == 1, err
}

func (s *store) update(ctx context.Context, name string, edit func(*profile) error) (profile, error) {
	for ctx.Err() == nil {
		p, err := s.get(ctx, name)
		if err != nil {
			return profile{}, err
		}
		if err := edit(&p); err != nil {
			return profile{}, err
		}
		ok, err := s.change(ctx, p)
		if err != nil {
			return profile{}, err
		}
		if ok {
			p.Rev++
			return p, nil
		}
	}
	return profile{}, ctx.Err()
}

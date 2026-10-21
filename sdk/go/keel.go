// Package keel is the runtime behind `keel sdk go`: the event envelope and the five Keel tools
// over HTTP (`keel serve --transport http`). Generated files add the cartridge's types.
package keel

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// Envelope is the standard event envelope. ID is optional: the server derives a content hash when absent.
type Envelope struct {
	ID         string `json:"id,omitempty"`
	Tenant     string `json:"tenant"`
	Type       string `json:"type"`
	Subject    string `json:"subject"`
	OccurredAt string `json:"occurred_at"`
	RecordedAt string `json:"recorded_at,omitempty"`
	Source     string `json:"source"`
	Payload    any    `json:"payload"`
}

// NewEnvelope builds an envelope; the generated Emit* functions call it.
func NewEnvelope(tenant, typ, subject string, occurredAt time.Time, source string, payload any) Envelope {
	return Envelope{Tenant: tenant, Type: typ, Subject: subject, OccurredAt: occurredAt.UTC().Format(time.RFC3339Nano), Source: source, Payload: payload}
}

type Entity struct {
	Tenant string         `json:"tenant"`
	Kind   string         `json:"kind"`
	ID     string         `json:"id"`
	Key    string         `json:"key"` // '<kind>:<id>', the subject every other call takes
	Ref    *string        `json:"ref"`
	Attrs  map[string]any `json:"attrs"`
}

type Fact struct {
	Tenant      string  `json:"tenant"`
	Subject     string  `json:"subject"`
	Predicate   string  `json:"predicate"`
	Value       any     `json:"value"`
	ValidFrom   string  `json:"valid_from"`
	ValidTo     *string `json:"valid_to"`
	RecordedAt  *string `json:"recorded_at"`
	Source      *string `json:"source"`
	Scope       string  `json:"scope"`
	CorrectedBy *string `json:"corrected_by"`
	Evidence    *string `json:"evidence"`
	ExpiresAt   *string `json:"expires_at"`
	Task        *string `json:"task,omitempty"`
	Key         *string `json:"idempotency_key,omitempty"`
	ClosedAt    *string `json:"closed_at,omitempty"` // transaction time of the close
	Status      *string `json:"status,omitempty"`    // nil = accepted, "proposed", "rejected"
	Trace       *string `json:"trace,omitempty"`
	Cartridge   *string `json:"cartridge,omitempty"` // annotations: name@version#rules they were made under
}

type RelationRow struct {
	Tenant     string  `json:"tenant"`
	Src        string  `json:"src"`
	Rel        string  `json:"rel"`
	Dst        string  `json:"dst"`
	ValidFrom  string  `json:"valid_from"`
	ValidTo    *string `json:"valid_to"`
	RecordedAt *string `json:"recorded_at"`
	Source     *string `json:"source"`
}

type Context struct {
	Subject            string                    `json:"subject"`
	Found              *bool                     `json:"found,omitempty"` // false: nothing is known about the subject (only Subject, AsOf, Watermark set)
	Entity             *Entity                   `json:"entity"`
	Facts              []Fact                    `json:"facts"`
	Relations          []RelationRow             `json:"relations"`
	Rules              map[string]map[string]any `json:"rules"`
	Evaluation         map[string]*bool          `json:"evaluation"`
	AsOf               string                    `json:"as_of"`
	AsKnownAt          string                    `json:"as_known_at,omitempty"`
	Watermark          *string                   `json:"watermark,omitempty"` // cursor for HistoryFilter.ChangedSince
	Proposals          []Fact                    `json:"proposals,omitempty"`
	Conflicts          map[string][]any          `json:"conflicts,omitempty"`
	Neighbors          []Neighbor                `json:"neighbors,omitempty"` // with ContextDepth(..., 1)
	NeighborsTruncated bool                      `json:"neighbors_truncated,omitempty"`
	RelationsMore      []CapNote                 `json:"relations_more,omitempty"` // relations left out of Relations (the most recent kept)
	NeighborsMore      []CapNote                 `json:"neighbors_more,omitempty"` // neighbours left out per relation (still in Relations)
}

// CapNote says what a cap cut: per relation and direction, how many are shown of how many.
type CapNote struct {
	Relation  string `json:"relation"`
	Direction string `json:"direction"`
	Shown     int    `json:"shown"`
	Total     int    `json:"total"`
}

// Neighbor is one linked subject with its open facts and rule evaluation (ContextDepth).
type Neighbor struct {
	Relation   string           `json:"relation"`
	Direction  string           `json:"direction"` // in, out or both
	Subject    string           `json:"subject"`
	Entity     *Entity          `json:"entity"`
	Facts      []Fact           `json:"facts"`
	Evaluation map[string]*bool `json:"evaluation"`
}

type Related struct {
	Relation  string `json:"relation"`
	Direction string `json:"direction"`
	Subject   string `json:"subject"`
	Since     string `json:"since"`
	Hops      int    `json:"hops,omitempty"` // RelatedWith Depth 2 or 3: how far
	Via       string `json:"via,omitempty"`  // and the subject it was reached from (Direction is relative to it)
}

type IngestReport struct {
	Processed  int              `json:"processed"`
	Duplicates int              `json:"duplicates"`
	Unhandled  []string         `json:"unhandled"`
	Errors     [][2]string      `json:"errors"`
	Forgotten  []map[string]any `json:"forgotten"` // tenants erased by a `forgets: tenant` event: {"tenant", "entities", ...}
	Skipped    int              `json:"skipped"`   // events for a forgotten tenant, never applied
	Retained   []map[string]any `json:"retained"`  // `forgets: before` events: {"tenant", "before", "facts", ...}
}

// Error is a non-2xx answer: a refusal (403: scope policy, agent registry), bad input (400), bad secret (401).
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string { return fmt.Sprintf("keel %d: %s", e.Status, e.Detail) }

// Options bind a Client to one server, one tenant and optionally one agent.
type Options struct {
	BaseURL     string
	Tenant      string
	Secret      string
	Agent       string
	Traceparent string // W3C trace context sent with every call; Keel stores it with every write
	HTTPClient  *http.Client
}

type Client struct{ o Options }

func NewClient(o Options) *Client {
	if o.HTTPClient == nil {
		o.HTTPClient = &http.Client{Timeout: 30 * time.Second}
	}
	o.BaseURL = strings.TrimRight(o.BaseURL, "/")
	return &Client{o: o}
}

func (c *Client) post(ctx context.Context, path string, body any, out any) error {
	raw, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.o.BaseURL+path, bytes.NewReader(raw))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Keel-Tenant", c.o.Tenant)
	if c.o.Agent != "" {
		req.Header.Set("X-Keel-Agent", c.o.Agent)
	}
	if c.o.Secret != "" {
		req.Header.Set("Authorization", "Bearer "+c.o.Secret)
	}
	if c.o.Traceparent != "" {
		req.Header.Set("traceparent", c.o.Traceparent)
	}
	res, err := c.o.HTTPClient.Do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		return err
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		var e struct {
			Detail any `json:"detail"`
		}
		detail := string(data)
		if json.Unmarshal(data, &e) == nil && e.Detail != nil {
			if s, ok := e.Detail.(string); ok {
				detail = s
			} else if b, err := json.Marshal(e.Detail); err == nil {
				detail = string(b)
			}
		}
		return &Error{Status: res.StatusCode, Detail: detail}
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}

// postList decodes a list as its type declares it: without the {"more"} (what a cap cut) or {"note"} entries the
// server may put first.
func postList[T any](c *Client, ctx context.Context, path string, body any) ([]T, error) {
	var raw []json.RawMessage
	if err := c.post(ctx, path, body, &raw); err != nil {
		return nil, err
	}
	return items[T](raw)
}

func items[T any](raw []json.RawMessage) ([]T, error) {
	for len(raw) > 0 {
		var m map[string]json.RawMessage
		if json.Unmarshal(raw[0], &m) != nil || len(m) != 1 || (m["more"] == nil && m["note"] == nil) {
			break
		}
		raw = raw[1:]
	}
	out := make([]T, len(raw))
	for i, r := range raw {
		if err := json.Unmarshal(r, &out[i]); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func iso(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := t.UTC().Format(time.RFC3339Nano)
	return &s
}

// Context: what is true about a subject now, or at asOf.
func (c *Client) Context(ctx context.Context, subject string, asOf *time.Time) (Context, error) {
	return c.ContextDepth(ctx, subject, asOf, 0)
}

// ContextDepth is Context with depth 1: each neighbour's entity, open facts and evaluation come along.
func (c *Client) ContextDepth(ctx context.Context, subject string, asOf *time.Time, depth int) (Context, error) {
	return c.ContextWith(ctx, subject, ContextOptions{AsOf: asOf, Depth: depth})
}

// ContextOptions: AsKnownAt answers what Keel knew then; Task keeps one task's memory; Exclude leaves predicates out;
// Only keeps just those predicates.
type ContextOptions struct {
	AsOf, AsKnownAt *time.Time
	Depth           int
	Task            string
	Exclude, Only   []string
}

func (o ContextOptions) body(subject any, detail string) map[string]any {
	body := map[string]any{"subject": subject, "as_of": iso(o.AsOf), "depth": o.Depth, "as_known_at": iso(o.AsKnownAt), "detail": detail} // "full", "concise" or "brief"
	if o.Task != "" {
		body["task"] = o.Task
	}
	if len(o.Exclude) > 0 {
		body["exclude"] = o.Exclude
	}
	if len(o.Only) > 0 {
		body["only"] = o.Only
	}
	return body
}

func (c *Client) ContextWith(ctx context.Context, subject string, o ContextOptions) (Context, error) {
	var out Context
	err := c.post(ctx, "/v1/context", o.body(subject, "full"), &out)
	return out, err
}

// Contexts is ContextWith for many subjects in one call; the answers come back in order.
func (c *Client) Contexts(ctx context.Context, subjects []string, o ContextOptions) ([]Context, error) {
	var out []Context
	err := c.post(ctx, "/v1/context", o.body(subjects, "full"), &out)
	return out, err
}

// ContextConcise is the same answer in about a third of the tokens, for an LLM prompt: facts as
// {predicate: value}, relations as {relation, direction, subject}, no rule definitions. Its shape
// differs from Context, so it comes back as a map.
func (c *Client) ContextConcise(ctx context.Context, subject string, o ContextOptions) (map[string]any, error) {
	var out map[string]any
	err := c.post(ctx, "/v1/context", o.body(subject, "concise"), &out)
	return out, err
}

// ContextBrief is ContextConcise with relations as {relation: [subjects]} (an incoming one as "<-relation"): the
// cheapest answer, what the HTTP API gives an agent by default.
func (c *Client) ContextBrief(ctx context.Context, subject string, o ContextOptions) (map[string]any, error) {
	var out map[string]any
	err := c.post(ctx, "/v1/context", o.body(subject, "brief"), &out)
	return out, err
}

type HistoryFilter struct {
	Since, Until            *time.Time
	Predicate               string
	Scope                   string
	AsKnownAt, ChangedSince *time.Time // ChangedSince with an empty subject: the tenant's changes since a watermark
	Task                    string
	Limit                   int    // the most recent n; 0 = every fact
	Detail                  string // "full" (default) or "concise": lean facts, without tenant, subject, recorded_at, closed_at and nulls
}

func (c *Client) History(ctx context.Context, subject string, f HistoryFilter) ([]Fact, error) {
	if f.Detail == "" {
		f.Detail = "full"
	}
	body := map[string]any{"since": iso(f.Since), "until": iso(f.Until), "as_known_at": iso(f.AsKnownAt), "changed_since": iso(f.ChangedSince), "limit": f.Limit, "detail": f.Detail}
	if subject != "" {
		body["subject"] = subject
	}
	if f.Predicate != "" {
		body["predicate"] = f.Predicate
	}
	if f.Scope != "" {
		body["scope"] = f.Scope
	}
	if f.Task != "" {
		body["task"] = f.Task
	}
	return postList[Fact](c, ctx, "/v1/history", body)
}

func (c *Client) Search(ctx context.Context, text string) ([]map[string]any, error) {
	return c.SearchWith(ctx, SearchOptions{Text: text})
}

// SearchOptions: with Kind or Rule it lists entities of a kind, or where a rule fires now, paged by Limit/After.
type SearchOptions struct {
	Text, Kind, Rule, After string
	Kinds                   []string       // several kinds at once, instead of Kind: listed together, ranked only among them, PerKind of each
	Limit                   int            // 0: the whole listing (no cap); a semantic ranking still returns its first 10
	Mode                    string         // "semantic": ranked by meaning, each result with a "score"
	MinScore                float64        // semantic: drop results whose cosine is below this; 0 = no floor
	RelatedTo, Via          string         // only RelatedTo's neighbours (through Via when set)
	Detail                  string         // "concise" or "brief" (SearchKeys for "key"): each result carries its current facts as {predicate: value}; "full": as facts, and fact hits whole (lean otherwise)
	Depth                   int            // 1: each result carries its neighbours
	PerKind                 int            // the best n of every kind: with RelatedTo and Mode "semantic", the context for a task
	Exclude                 []string       // predicates left out of what is returned
	AsOf                    *time.Time     // rules, neighbours and facts at this instant
	AsKnownAt               *time.Time     // as Keel knew them at this instant (transaction time); AsOf defaults to it
	Where                   map[string]any // only entities whose attribute or current fact has this value (or a list holding it)
}

func (o SearchOptions) body(text any) map[string]any {
	body := map[string]any{"text": text, "limit": o.Limit}
	for k, v := range map[string]string{"kind": o.Kind, "rule": o.Rule, "after": o.After, "mode": o.Mode, "related_to": o.RelatedTo, "via": o.Via, "detail": o.Detail} {
		if v != "" {
			body[k] = v
		}
	}
	for k, v := range map[string]int{"depth": o.Depth, "per_kind": o.PerKind} {
		if v > 0 {
			body[k] = v
		}
	}
	if o.MinScore > 0 {
		body["min_score"] = o.MinScore
	}
	if len(o.Exclude) > 0 {
		body["exclude"] = o.Exclude
	}
	if o.AsOf != nil {
		body["as_of"] = iso(o.AsOf)
	}
	if o.AsKnownAt != nil {
		body["as_known_at"] = iso(o.AsKnownAt)
	}
	if len(o.Where) > 0 {
		body["where"] = o.Where
	}
	if len(o.Kinds) > 0 {
		body["kind"] = o.Kinds
	}
	return body
}

func (c *Client) SearchWith(ctx context.Context, o SearchOptions) ([]map[string]any, error) {
	return postList[map[string]any](c, ctx, "/v1/search", o.body(o.Text))
}

// Count is how many results SearchWith would list (detail "count"), with no cap and nothing listed.
// o.Detail, o.Depth, o.Limit, o.After and o.PerKind are ignored.
func (c *Client) Count(ctx context.Context, o SearchOptions) (int, error) {
	o.Detail, o.Depth, o.Limit, o.After, o.PerKind = "count", 0, 0, "", 0
	out, err := postList[map[string]any](c, ctx, "/v1/search", o.body(o.Text))
	if err != nil {
		return 0, err
	}
	for _, x := range out {
		if n, ok := x["count"].(float64); ok {
			return int(n), nil
		}
	}
	return 0, nil
}

// SearchKeys is SearchWith returning only the keys (detail "key"), each once: the cheapest listing.
// o.Detail and o.Depth are ignored.
func (c *Client) SearchKeys(ctx context.Context, o SearchOptions) ([]string, error) {
	o.Detail, o.Depth = "key", 0
	return postList[string](c, ctx, "/v1/search", o.body(o.Text))
}

// Searches runs one search per text in one call (o.Text is ignored); one result list per text, in order.
func (c *Client) Searches(ctx context.Context, texts []string, o SearchOptions) ([][]map[string]any, error) {
	var raw [][]json.RawMessage
	if err := c.post(ctx, "/v1/search", o.body(texts), &raw); err != nil {
		return nil, err
	}
	out := make([][]map[string]any, len(raw))
	for i, list := range raw {
		var err error
		if out[i], err = items[map[string]any](list); err != nil {
			return nil, err
		}
	}
	return out, nil
}

type Annotation struct {
	Subject   string `json:"subject"`
	Predicate string `json:"predicate"`
	Value     any    `json:"value"`
	Scope     string `json:"scope,omitempty"`
	Evidence  string `json:"evidence,omitempty"`
	By        string `json:"by,omitempty"`
	Task      string `json:"task,omitempty"`
	Key       string `json:"key,omitempty"`    // a retried write with the same key returns the first fact
	Status    string `json:"status,omitempty"` // "proposed" or "rejected"; empty = accepted
	Detail    string `json:"detail,omitempty"` // "full" (default): the whole fact; "ack": a short receipt
}

func (c *Client) Annotate(ctx context.Context, a Annotation) (Fact, error) {
	if a.Detail == "" {
		a.Detail = "full"
	}
	var out Fact
	err := c.post(ctx, "/v1/annotate", a, &out)
	return out, err
}

func (c *Client) Related(ctx context.Context, subject, relation string) ([]Related, error) {
	return c.RelatedAt(ctx, subject, relation, nil)
}

// RelatedAt is Related at an instant in world time.
func (c *Client) RelatedAt(ctx context.Context, subject, relation string, asOf *time.Time) ([]Related, error) {
	o := RelatedOptions{AsOf: asOf}
	if relation != "" {
		o.Relations = []string{relation}
	}
	return c.RelatedWith(ctx, subject, o)
}

// RelatedOptions: Relations filters by type (any of them); Depth 2 or 3 walks further through them; Limit keeps the
// nearest, most recent n (0 = every neighbour).
type RelatedOptions struct {
	Relations []string
	AsOf      *time.Time
	Depth     int
	Limit     int
}

// RelatedWith walks up to Depth hops; with Depth above 1 every entry carries Hops and Via.
func (c *Client) RelatedWith(ctx context.Context, subject string, o RelatedOptions) ([]Related, error) {
	body := map[string]any{"subject": subject, "as_of": iso(o.AsOf), "limit": o.Limit}
	if len(o.Relations) == 1 {
		body["relation"] = o.Relations[0]
	} else if len(o.Relations) > 1 {
		body["relation"] = o.Relations
	}
	if o.Depth > 1 {
		body["depth"] = o.Depth
	}
	return postList[Related](c, ctx, "/v1/related", body)
}

// Events ingests a batch. At-least-once is yours: retry on network errors.
func (c *Client) Events(ctx context.Context, batch []Envelope) (IngestReport, error) {
	var out IngestReport
	err := c.post(ctx, "/v1/events", batch, &out)
	return out, err
}

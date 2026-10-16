package fleet

// End to end from Go: start `keel serve --transport http` (Python), emit typed events, ask the tools.

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	keel "github.com/angelkurten/keel/sdk/go"
)

const secret = "s3cret"

func startServer(t *testing.T) string {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := l.Addr().(*net.TCPAddr).Port
	l.Close()
	root, _ := filepath.Abs("../../..")
	cmd := exec.Command("uv", "run", "keel", "serve", "examples/fleet", "--transport", "http", "--port", fmt.Sprint(port))
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "KEEL_SECRET="+secret)
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = cmd.Process.Kill() })
	base := fmt.Sprintf("http://127.0.0.1:%d", port)
	for i := 0; i < 200; i++ {
		if res, err := http.Get(base + "/health"); err == nil && res.StatusCode == 200 {
			res.Body.Close()
			return base
		}
		time.Sleep(50 * time.Millisecond)
	}
	t.Fatal("keel serve did not come up")
	return ""
}

func TestGeneratedClient(t *testing.T) {
	base := startServer(t)
	ctx := context.Background()
	tenant := "acme-logistics"
	c := NewClient(keel.Options{BaseURL: base, Tenant: tenant, Secret: secret})
	t1, _ := time.Parse(time.RFC3339, "2026-09-10T08:00:00Z")
	t2, _ := time.Parse(time.RFC3339, "2026-10-03T08:00:00Z")
	report, err := c.Events(ctx, []keel.Envelope{
		EmitUnitRegistered(tenant, SubjectUnit("MXK-4821"), t1, "fleet.vehicles", UnitRegisteredPayload{Id: "MXK-4821", Plate: "MXK-4821", Model: "Cascadia"}),
		EmitTireDepthMeasured(tenant, SubjectTire("T-9001"), t1, "tires", TireDepthMeasuredPayload{UnitId: "MXK-4821", Position: "P3", DepthMm: 5.2}),
		EmitTireDepthMeasured(tenant, SubjectTire("T-9001"), t2, "tires", TireDepthMeasuredPayload{UnitId: "MXK-4821", Position: "P3", DepthMm: 2.6}),
	})
	if err != nil || report.Processed != 3 || len(report.Errors) != 0 {
		t.Fatalf("ingest: %v %+v", err, report)
	}
	got, err := c.Context(ctx, string(SubjectTire("T-9001")), nil)
	if err != nil || got.Evaluation["tire_critical"] == nil || !*got.Evaluation["tire_critical"] {
		t.Fatalf("context: %v %+v", err, got.Evaluation)
	}
	asOf, _ := time.Parse(time.RFC3339, "2026-09-20T00:00:00Z")
	before, err := c.Context(ctx, string(SubjectTire("T-9001")), &asOf)
	if err != nil || *before.Evaluation["tire_critical"] {
		t.Fatalf("as_of: %v %+v", err, before.Evaluation)
	}
	depths, err := c.History(ctx, string(SubjectTire("T-9001")), keel.HistoryFilter{Predicate: "depth_mm"})
	if err != nil || len(depths) != 2 || depths[0].Value != 5.2 || depths[1].Value != 2.6 {
		t.Fatalf("history: %v %+v", err, depths)
	}
	rel, err := c.Related(ctx, string(SubjectUnit("MXK-4821")), "mounted_on")
	if err != nil || len(rel) != 1 || rel[0].Subject != "tire:T-9001" {
		t.Fatalf("related: %v %+v", err, rel)
	}
	if hits, err := c.Search(ctx, "T-9001"); err != nil || len(hits) == 0 {
		t.Fatalf("search: %v %+v", err, hits)
	}
	if note, err := c.Annotate(ctx, keel.Annotation{Subject: "tire:T-9001", Predicate: "assessment", Value: "replace", Scope: "task", By: "agent:go"}); err != nil || note.Scope != "task" {
		t.Fatalf("annotate: %v %+v", err, note)
	}
	_, err = c.Annotate(ctx, keel.Annotation{Subject: "tire:T-9001", Predicate: "assessment", Value: "x", Scope: "tenant"})
	var ke *keel.Error
	if !errors.As(err, &ke) || ke.Status != 403 {
		t.Fatalf("expected a 403 refusal, got %v", err)
	}
	first, err := c.Annotate(ctx, keel.Annotation{Subject: "tire:T-9001", Predicate: "plan", Value: "swap", Task: "run-1", Key: "k1"})
	again, err2 := c.Annotate(ctx, keel.Annotation{Subject: "tire:T-9001", Predicate: "plan", Value: "swap", Task: "run-1", Key: "k1"})
	if err != nil || err2 != nil || *first.RecordedAt != *again.RecordedAt {
		t.Fatalf("idempotent annotate: %v %v", err, err2)
	}
	epoch := time.Date(2000, 1, 1, 0, 0, 0, 0, time.UTC)
	if changes, err := c.History(ctx, "", keel.HistoryFilter{ChangedSince: &epoch, Task: "run-1"}); err != nil || len(changes) != 1 {
		t.Fatalf("changes: %v %+v", err, changes)
	}
	if tires, err := c.SearchWith(ctx, keel.SearchOptions{Kind: "tire"}); err != nil || len(tires) != 1 {
		t.Fatalf("search kind: %v %+v", err, tires)
	}
	if p, err := c.Annotate(ctx, keel.Annotation{Subject: "tire:T-9001", Predicate: "swap", Value: true, Status: "proposed"}); err != nil || p.Status == nil || *p.Status != "proposed" {
		t.Fatalf("proposal: %v %+v", err, p)
	}
	if got, err := c.ContextConcise(ctx, "tire:T-9001", keel.ContextOptions{}); err != nil || got["facts"].(map[string]any)["depth_mm"] != 2.6 {
		t.Fatalf("concise: %v %+v", err, got)
	}
	// the context for a task (0.19): facts and neighbours on each result, batches, hops, hidden predicates
	if hits, err := c.SearchWith(ctx, keel.SearchOptions{Text: "T-9001", Detail: "concise"}); err != nil || hits[0]["facts"].(map[string]any)["depth_mm"] != 2.6 {
		t.Fatalf("search detail: %v %+v", err, hits)
	}
	if hits, err := c.SearchWith(ctx, keel.SearchOptions{Kind: "tire", Where: map[string]any{"depth_mm": 2.6}}); err != nil || len(hits) != 1 || hits[0]["key"] != "tire:T-9001" {
		t.Fatalf("search where: %v %v", hits, err)
	}
	if hits, err := c.SearchWith(ctx, keel.SearchOptions{Kinds: []string{"unit", "tire"}, Where: map[string]any{"depth_mm": 2.6}}); err != nil || len(hits) != 1 || hits[0]["key"] != "tire:T-9001" {
		t.Fatalf("search kinds: %v %v", hits, err)
	}
	if hits, err := c.SearchWith(ctx, keel.SearchOptions{Text: "2.6", Kind: "tire"}); err != nil || len(hits) != 1 || hits[0]["matched"] != "depth_mm" || hits[0]["snippet"] != "2.6" {
		t.Fatalf("search matched: %v %v", hits, err)
	}
	if units, err := c.SearchWith(ctx, keel.SearchOptions{Kind: "unit", Depth: 1}); err != nil || len(units[0]["neighbors"].([]any)) == 0 {
		t.Fatalf("search depth: %v %+v", err, units)
	}
	both, err := c.Contexts(ctx, []string{"tire:T-9001", "unit:MXK-4821"}, keel.ContextOptions{Exclude: []string{"depth_mm"}})
	if err != nil || len(both) != 2 || both[1].Subject != "unit:MXK-4821" {
		t.Fatalf("contexts: %v %+v", err, both)
	}
	for _, f := range both[0].Facts {
		if f.Predicate == "depth_mm" {
			t.Fatalf("exclude: depth_mm returned")
		}
	}
	if just, err := c.Contexts(ctx, []string{"tire:T-9001", "unit:MXK-4821"}, keel.ContextOptions{Only: []string{"depth_mm"}}); err != nil || len(just[0].Facts) != 1 || just[0].Facts[0].Predicate != "depth_mm" || len(just[1].Facts) != 0 {
		t.Fatalf("only: %v %v", just, err)
	}
	if lists, err := c.Searches(ctx, []string{"T-9001", "MXK"}, keel.SearchOptions{PerKind: 1}); err != nil || len(lists) != 2 {
		t.Fatalf("searches: %v %+v", err, lists)
	}
	if _, err := c.Events(ctx, []keel.Envelope{EmitDriverAssigned(tenant, SubjectUnit("MXK-4821"), t1, "core", DriverAssignedPayload{DriverId: "D-118"})}); err != nil {
		t.Fatalf("driver: %v", err)
	}
	hops, err := c.RelatedWith(ctx, "tire:T-9001", keel.RelatedOptions{Relations: []string{"mounted_on", "assigned_to"}, Depth: 2})
	found := false
	for _, r := range hops {
		found = found || (r.Subject == "driver:D-118" && r.Hops == 2 && r.Via == "unit:MXK-4821")
	}
	if err != nil || !found {
		t.Fatalf("related depth: %v %+v", err, hops)
	}
	other := NewClient(keel.Options{BaseURL: base, Tenant: "someone-else", Secret: secret})
	if got, err := other.Context(ctx, "tire:T-9001", nil); err != nil || got.Entity != nil {
		t.Fatalf("tenant isolation: %v %+v", err, got.Entity)
	}
	_, err = NewClient(keel.Options{BaseURL: base, Tenant: tenant, Secret: "wrong"}).Search(ctx, "x")
	if !errors.As(err, &ke) || ke.Status != 401 {
		t.Fatalf("expected 401, got %v", err)
	}
	if Cartridge != "fleet" || len(Kinds) != 7 {
		t.Fatalf("constants: %s %v", Cartridge, Kinds)
	}
}

# github.com/angelkurten/keel/sdk/go

Runtime for the clients `keel sdk go <cartridge> --package <pkg>` generates: the event envelope and the five Keel tools over HTTP (`keel serve --transport http`).

```bash
go get github.com/angelkurten/keel/sdk/go
keel sdk go cartridges/fleet -o internal/keelfleet/keel_fleet.go --package keelfleet
```

```go
c := keelfleet.NewClient(keel.Options{BaseURL: "http://keel.internal:8000", Tenant: "acme-logistics", Secret: os.Getenv("KEEL_SECRET")})
_, err := c.Events(ctx, []keel.Envelope{
    keelfleet.EmitTireDepthMeasured("acme-logistics", keelfleet.SubjectTire("T-9001"), time.Now(), "tires", keelfleet.TireDepthMeasuredPayload{UnitId: "MXK-4821", Position: "P3", DepthMm: 2.6}),
})
got, err := c.Context(ctx, "tire:T-9001", nil)
*got.Evaluation["tire_critical"] // true
```

A refusal from the server is a `*keel.Error` with `Status` and `Detail`. The generated file is `go vet` and `gofmt` clean; keep it fresh with `keel sdk go ... --check` in CI.

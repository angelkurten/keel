import type { Me } from "../api";
import { Icon } from "../ui";

export function Starting({ me }: { me: Me }) {
  const failed = me.status === "failed";
  const updating = me.status === "updating"; // a new cartridge replacing the running one
  return (
    <div className="entry top">
      <div className="entry-col wide">
        <div className="stack-s">
          <span className="muted small-text">Signed in as {me.email}</span>
          {updating ? <h1 className="h1">Deploying your cartridge</h1>
            : <h1 className="h1">{failed ? "Setup failed for " : "Setting up "}<span className="mono">{me.account}</span></h1>}
        </div>
        <ol className="card stepper">
          {me.steps.map((s) => (
            <li key={s.id} className={s.state}>
              <span className="mark">
                {s.state === "done" ? <Icon name="check" size={14} />
                  : s.state === "active" ? <span className="spin" />
                  : s.state === "failed" ? <Icon name="x" size={14} />
                  : <span className="ring" />}
              </span>
              <span className="label">{s.label}</span>
            </li>
          ))}
        </ol>
        {failed ? (
          <div className="card danger">
            <p className="err">{me.error || "Something went wrong while creating your Keel."}</p>
            <p className="muted">Write to <a href="mailto:angel@angelkurten.com">angel@angelkurten.com</a> and we'll sort it out.</p>
          </div>
        ) : updating ? null : (
          <div className="dashed">
            <span className="eyebrow">While you wait: what you'll get</span>
            <div className="grid3">
              <div><b>A graph per tenant</b><p className="muted">Entities, relations and facts with time, built from events.</p></div>
              <div><b>Five tools</b><p className="muted">context, history, search, annotate, related.</p></div>
              <div><b>MCP endpoint</b><p className="muted">Any agent connects with one command.</p></div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

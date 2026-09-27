import ModelDownloads from "../components/ModelDownloads";
import Orb from "../components/Orb";

/** Before anything else: the on-device models (downloaded once, then fully offline). */
export default function FirstRun() {
  return (
    <div className="setup firstrun-page">
      <section className="setup-card wide firstrun">
        <header className="firstrun-head">
          <Orb mode="idle" className="firstrun-orb" />
          <div className="firstrun-copy">
            <span className="firstrun-kicker">SYSTEM INITIALISATION · ON-DEVICE MODELS</span>
            <h1>First, the models.</h1>
            <p className="lead">Everything I do runs on this Mac, so I need my models before we start.</p>
            <p className="muted small">
              One download, from the projects that publish them. Each file is checked against its SHA-256 before it is
              used, and an interrupted download picks up where it stopped. After this, I work offline.
            </p>
          </div>
        </header>
        <ModelDownloads firstRun />
      </section>
    </div>
  );
}

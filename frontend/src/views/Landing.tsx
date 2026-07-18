import { useUi } from '../store/ui';

export default function Landing() {
  const showView = useUi((s) => s.showView);
  const switchTab = useUi((s) => s.switchTab);

  // WizardView loads the persisted wizard state itself on mount
  const openWizard = () => showView('wizard');

  return (
    <div className="landing-container">
      <div className="landing-hero">
        <h2>Real-time Linux Performance Profiler</h2>
        <p className="landing-sub">Connect to a remote agent, profile a process, explore results</p>
      </div>
      <div className="landing-cards">
        <div className="landing-card" id="card-live" data-testid="card-live" onClick={openWizard}>
          <div className="landing-card-icon">&#9881;</div>
          <h3>Live Debug</h3>
          <p>Connect to an agent running on your target device and start a new profiling session</p>
        </div>
        <div className="landing-card" id="card-sessions" data-testid="card-sessions"
             onClick={() => { showView('profiling'); switchTab('sessions'); }}>
          <div className="landing-card-icon">&#128193;</div>
          <h3>Saved Sessions</h3>
          <p>Browse, replay, or import previously saved profiling sessions</p>
        </div>
      </div>
      <div className="landing-agent-note" id="landing-agent-note">
        Agents using <code>--server</code> connect in automatically &mdash; the UI will switch when
        an agent arrives
      </div>
    </div>
  );
}

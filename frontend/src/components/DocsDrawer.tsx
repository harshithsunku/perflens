import { useEffect, useState } from 'react';
import { DOCS_HTML, DOCS_TABS } from './docsContent';

interface Props {
  open: boolean;
  onClose: () => void;
}

export default function DocsDrawer({ open, onClose }: Props) {
  const [tab, setTab] = useState('start');
  // Two-phase visibility so the slide transition runs (docs-closed keeps
  // it out of the layout; visible animates it in)
  const [mounted, setMounted] = useState(false);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (open) {
      setMounted(true);
      const t = requestAnimationFrame(() => setVisible(true));
      return () => cancelAnimationFrame(t);
    }
    setVisible(false);
    const t = setTimeout(() => setMounted(false), 300);
    return () => clearTimeout(t);
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const cls = (base: string) =>
    base + (mounted ? '' : ' docs-closed') + (visible ? ' visible' : '');

  return (
    <>
      <div id="docs-overlay" className={cls('docs-overlay')} onClick={onClose}></div>
      <div id="docs-drawer" className={cls('docs-drawer')}>
        <div className="docs-header">
          <h2>Documentation</h2>
          <span className="docs-version" id="docs-version">v0.8.0</span>
          <div className="docs-header-spacer"></div>
          <button id="docs-close" className="docs-close" onClick={onClose}>&times;</button>
        </div>
        <div className="docs-tabs">
          {DOCS_TABS.map((t) => (
            <button key={t.id} className={'docs-tab' + (tab === t.id ? ' active' : '')}
                    data-docs-tab={t.id} onClick={() => setTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>
        <div className="docs-body">
          <div className="docs-panel active" data-docs-panel={tab}
               dangerouslySetInnerHTML={{ __html: DOCS_HTML[tab] ?? '' }} />
        </div>
      </div>
    </>
  );
}

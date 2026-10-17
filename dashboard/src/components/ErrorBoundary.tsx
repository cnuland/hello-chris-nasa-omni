// Keeps the app shell (nav, toasts) alive when a page throws during render, e.g. on unexpected (untrusted) data.
import { Component, type ErrorInfo, type ReactNode } from 'react';
import { log } from '../lib/log';

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    log.error('page render failed', { error: String(error), stack: info.componentStack ?? '' });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div className="card error-panel" role="alert">
        <h1>Something went wrong</h1>
        <p>{`This page could not be displayed: ${error.message || String(error)}`}</p>
        <p className="muted">Use the navigation to continue, or reload the page.</p>
        <button type="button" onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}

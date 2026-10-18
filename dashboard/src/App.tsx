// Routes (see docs/tdd-log/dashboard.md "UI contract"):
//   /            Inventory (AC-UI-1..3)         /items/new   Add item (AC-UI-6)
//   /items/:id   Item detail + edit (AC-UI-4/5) /review      Review queue (AC-UI-7)
//   /chat        Chat (AC-UI-8)                 /overview    Overview (AC-UI-9)
// App renders inside a Router supplied by main.tsx (BrowserRouter) or tests (MemoryRouter).
import type React from 'react';
import { NavLink, Route, Routes, useLocation } from 'react-router-dom';
import { ErrorBoundary } from './components/ErrorBoundary';
import AddItemPage from './pages/AddItemPage';
import ChatPage from './pages/ChatPage';
import InventoryPage from './pages/InventoryPage';
import ItemDetailPage from './pages/ItemDetailPage';
import OverviewPage from './pages/OverviewPage';
import ReviewPage from './pages/ReviewPage';
import { ReviewCountProvider, useReviewCount } from './state/reviewCount';
import { ToastProvider } from './state/toasts';

function Nav() {
  const { count } = useReviewCount();
  const cls = ({ isActive }: { isActive: boolean }) => (isActive ? 'nav-link active' : 'nav-link');
  return (
    <nav className="nav" aria-label="Primary">
      <NavLink to="/" end className={cls}>Inventory</NavLink>
      <NavLink to="/overview" className={cls}>Overview</NavLink>
      <NavLink to="/review" className={cls}>
        Review
        {count !== null && (
          <span className={count > 0 ? 'badge badge-alert' : 'badge'} title={`${count} open review items`}>
            {` ${count}`}
          </span>
        )}
      </NavLink>
      <NavLink to="/chat" className={cls}>Chat</NavLink>
    </nav>
  );
}

/** Each route gets a fresh boundary (keyed by path), so navigating away from a crashed page recovers. */
function RouteBoundary({ children }: { children: React.ReactNode }) {
  const { pathname } = useLocation();
  return <ErrorBoundary key={pathname}>{children}</ErrorBoundary>;
}

function NotFound() {
  // UI3-12: every page has an h1 (axe page-has-heading-one).
  return (
    <div className="page">
      <h1>Page not found</h1>
      <p className="empty">There is no page at this address.</p>
    </div>
  );
}

export default function App() {
  return (
    <ToastProvider>
      <ReviewCountProvider>
        <div className="shell">
          <a className="skip-link" href="#main">Skip to content</a>
          <header className="topbar">
            <div className="brand">
              <span className="brand-mark" aria-hidden="true">◉</span>
              <span>ISS Inventory</span>
              <span className="brand-sub">mission control</span>
            </div>
            <Nav />
          </header>
          <main className="content" id="main" tabIndex={-1}>
            <RouteBoundary>
            <Routes>
              <Route path="/" element={<InventoryPage />} />
              <Route path="/items/new" element={<AddItemPage />} />
              <Route path="/items/:id" element={<ItemDetailPage />} />
              <Route path="/review" element={<ReviewPage />} />
              <Route path="/chat" element={<ChatPage />} />
              <Route path="/overview" element={<OverviewPage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
            </RouteBoundary>
          </main>
        </div>
      </ReviewCountProvider>
    </ToastProvider>
  );
}

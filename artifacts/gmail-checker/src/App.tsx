import { useEffect } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { Route, Switch, Router as WouterRouter } from 'wouter';
import Home from './pages/home';
import SmtpChecker from './pages/SmtpChecker';

// Port 8888 is the original SMTP checker. This Gmail project is served on 8890.
function isSmtpSite() {
  return window.location.port === "8888";
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
    },
  },
});

function NotFound() {
  return (
    <div className="min-h-screen w-full flex items-center justify-center bg-background text-foreground">
      <div className="text-center font-mono space-y-4">
        <h1 className="text-4xl font-bold text-primary">404</h1>
        <p className="text-sm text-muted-foreground uppercase tracking-widest">
          Route Not Found
        </p>
      </div>
    </div>
  );
}

function Router() {
  return (
    <Switch>
      <Route path="/" component={isSmtpSite() ? SmtpChecker : Home} />
      <Route component={NotFound} />
    </Switch>
  );
}

function App() {
  useEffect(() => {
    document.title = isSmtpSite() ? "SMTP Check" : "Gmail Checker";
  }, []);
  return (
    <QueryClientProvider client={queryClient}>
      <WouterRouter base={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        <Router />
      </WouterRouter>
    </QueryClientProvider>
  );
}

export default App;

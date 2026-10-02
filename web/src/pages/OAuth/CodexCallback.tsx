import { useEffect } from 'react';
import { useNavigate } from 'react-router';

/**
 * Legacy OAuth callback page — no longer used with device code flow.
 * Redirects to dashboard.
 */
export default function CodexCallback() {
  const navigate = useNavigate();
  useEffect(() => {
    navigate('/dashboard', { replace: true });
  }, [navigate]);
  return null;
}

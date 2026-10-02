import { AlertTriangle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useWorkspaceOptions } from '../hooks/useWorkspaceOptions';
import { RowNote } from './RowNote';

/**
 * Why the scope controls are missing when the workspace list failed to load.
 * They stay out rather than count a reach against no list, so without this the
 * tab reads as one where nothing has a scope.
 */
export function WorkspacesLoadNote() {
  const { t } = useTranslation();
  const { loadFailed, retry } = useWorkspaceOptions();
  if (!loadFailed) return null;
  return (
    <div className="mb-3">
      <RowNote icon={AlertTriangle}>
        {t('plugins.scope.workspacesLoadFailed')}{' '}
        <button
          type="button"
          onClick={retry}
          className="underline underline-offset-2 hover:text-(--color-text-secondary)"
        >
          {t('common.retry')}
        </button>
      </RowNote>
    </div>
  );
}

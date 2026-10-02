const version = import.meta.env.VITE_APP_VERSION || '0.0.0-dev'
const commitSha = import.meta.env.VITE_GIT_COMMIT || 'unknown'
const shortCommitSha = commitSha === 'unknown' ? commitSha : commitSha.slice(0, 7)

/** 현재 화면을 만든 앱 버전과 Git 커밋을 작게 표시한다. */
export function VersionBadge() {
  const label = `v${version} · ${shortCommitSha}`

  return (
    <p className="version-badge" title={`EviDrug ${label} (${commitSha})`} aria-label={`앱 버전 ${label}`}>
      {label}
    </p>
  )
}

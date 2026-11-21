import { request } from './client'

export type IdentityCandidate = {
  table: string
  column: string
  matched_by: string[]
  aliases: string[]
  effective_from: string
  effective_to: string | null
}

export type ReadPath = {
  edge_id: number
  source: string
  target: string
  field: string
  direction: string
  transform: string
  active: boolean
  basis?: string
}

export type ImpactSummary = {
  basis: string
  downstream_targets: string[]
  read_paths: ReadPath[]
  write_tasks: {
    task_name: string
    pipeline: string
    target_table: string
    source: string
  }[]
  affected_quality_rules: {
    rule_name: string
    target_table: string
    field: string
    status: string
  }[]
  affected_pipelines: string[]
  total_impact_items: number
}

export type IdentityResult = {
  query: { table: string; effective_date: string; name: string }
  resolved: boolean
  ambiguous: boolean
  production_table: string | null
  production_column: string | null
  identities: { id: number; table: string; column: string; effective_from: string; effective_to: string | null }[]
  aliases: { id: number; alias: string; column: string; effective_from: string; effective_to: string | null }[]
  candidates: IdentityCandidate[]
  read_paths: ReadPath[]
  lineage_chain: { target_table: string; depth: number; path: Record<string, unknown>[] }[]
  impact: ImpactSummary
  message: string
}

export type ImpactEntry = {
  id: number
  task_name: string
  pipeline: string
  production_table: string
  field: string
  target_table: string
  change_type: string
  effective_date: string
  status: string
  可提交: boolean
  缺失证据: string[]
  source: string
  candidate?: IdentityCandidate
}

export type ImpactBatch = {
  generated?: boolean
  query: { table: string; effective_date: string; name: string }
  resolved: boolean
  ambiguous: boolean
  production_table: string | null
  production_column: string | null
  impact?: ImpactSummary
  message: string
  entries: ImpactEntry[]
  submittable_count: number
  blocked_count: number
  change?: Record<string, unknown>
}

export type AliasRecord = {
  id: number
  table: string
  alias: string
  column: string
  effective_from: string
  effective_to: string | null
  revision: number
  superseded_by: number | null
  note: string
}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  const response = await request(path, init)
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(payload?.detail ?? `接口返回 ${response.status}`)
  }
  return payload as T
}

const json = (body: unknown): RequestInit => ({
  method: 'POST',
  body: JSON.stringify(body),
})

export function resolveIdentity(params: {
  table?: string
  name?: string
  effective_date: string
}): Promise<IdentityResult> {
  const query = new URLSearchParams()
  if (params.table) query.set('table', params.table)
  if (params.name) query.set('name', params.name)
  query.set('effective_date', params.effective_date)
  return send<IdentityResult>(`/api/field-identities/resolve?${query.toString()}`, {})
}

export function registerField(body: Record<string, string>): Promise<{ ok: boolean; message: string }> {
  return send('/api/field-identities/fields', json(body))
}

export function registerAlias(body: Record<string, string>): Promise<{ ok: boolean; message: string }> {
  return send('/api/field-identities/aliases', json(body))
}

export type Recalculation = {
  id: number
  affected_write_targets: string[]
  write_results: { task_name: string; pipeline: string; target_table: string; field: string }[]
  history_snapshot: { query_date: string; table: string; column: string; preserved: boolean }
}

export function reviseAlias(
  aliasId: number,
  body: Record<string, string>,
): Promise<{ ok: boolean; message: string; recalculation: Recalculation; alias: AliasRecord }> {
  return send(`/api/field-identities/aliases/${aliasId}/revisions`, json(body))
}

export function aliasHistory(table?: string): Promise<{ items: AliasRecord[]; total: number }> {
  const query = new URLSearchParams()
  if (table) query.set('table', table)
  return send(`/api/field-identities/aliases/history?${query.toString()}`, {})
}

export function generateImpactEntries(body: Record<string, unknown>): Promise<ImpactBatch> {
  return send('/api/field-identities/impact-entries', json(body))
}

export function generateFromChange(body: Record<string, unknown>): Promise<ImpactBatch> {
  return send('/api/field-identities/impact-entries/from-change', json(body))
}

export function listImpactEntries(table?: string): Promise<{
  entries: ImpactEntry[]
  submittable: number[]
  blocked: { entry_id: number; status: string; missing_evidence: string[] }[]
}> {
  const query = new URLSearchParams()
  if (table) query.set('table', table)
  return send(`/api/field-identities/impact-entries?${query.toString()}`, {})
}

export function confirmEntry(entryId: number): Promise<{ ok: boolean; message: string }> {
  return send(`/api/field-identities/impact-entries/${entryId}/confirm`, json({}))
}

export function candidateColumns(params: { table?: string; column?: string } = {}): Promise<{
  ok: boolean
  count: number
  message: string
  candidates: {
    entry_id: number
    field: string
    production_table: string
    target_table: string
    pipeline: string
    task_name: string
    effective_date: string
  }[]
}> {
  const query = new URLSearchParams()
  if (params.table) query.set('table', params.table)
  if (params.column) query.set('column', params.column)
  return send(`/api/field-identities/candidate-columns?${query.toString()}`, {})
}

export function runDrill(entryIds: number[]): Promise<{
  ok: boolean
  message: string
  count: number
  write_results: { task_name: string; pipeline: string; target_table: string; field: string; result: string }[]
}> {
  return send('/api/field-identities/drill', json({ entry_ids: entryIds }))
}

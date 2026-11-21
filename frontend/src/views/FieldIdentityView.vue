<template>
  <section class="page field-identity-page" data-testid="field-identity-page">
    <header class="page-head">
      <div>
        <h2>字段身份与别名有效期</h2>
        <p class="page-desc">
          按表和生效日期核实字段身份，定位读取路径与写入影响；同名候选不猜来源，
          影响单里分清可提交条目与还欠表/日期证据的条目。
        </p>
      </div>
    </header>

    <div class="fi-grid">
      <!-- 1. 登记 -->
      <article class="fi-card">
        <h3>登记字段身份</h3>
        <p class="fi-hint">表 + 列 + 生效期确定唯一身份；同表同列的有效期不能重叠。</p>
        <form data-testid="field-form" @submit.prevent="submitField">
          <label>表名<input v-model="fieldForm.table" data-testid="field-table" placeholder="ods.ods_order_info" required /></label>
          <label>列名<input v-model="fieldForm.column" data-testid="field-column" placeholder="order_no" required /></label>
          <label>生效日期<input v-model="fieldForm.effective_from" data-testid="field-from" type="date" required /></label>
          <label>失效日期<input v-model="fieldForm.effective_to" data-testid="field-to" type="date" /></label>
          <button class="btn primary" data-testid="field-submit" type="submit">登记列</button>
        </form>
      </article>

      <article class="fi-card">
        <h3>登记旧别名</h3>
        <p class="fi-hint">别名有效期必须落在同表目标列的生效期内，不能覆盖已生效历史。</p>
        <form data-testid="alias-form" @submit.prevent="submitAlias">
          <label>表名<input v-model="aliasForm.table" data-testid="alias-table" placeholder="ods.ods_order_info" required /></label>
          <label>旧别名<input v-model="aliasForm.alias" data-testid="alias-alias" placeholder="order_code" required /></label>
          <label>目标列<input v-model="aliasForm.column" data-testid="alias-column" placeholder="order_no" required /></label>
          <label>生效日期<input v-model="aliasForm.effective_from" data-testid="alias-from" type="date" required /></label>
          <label>失效日期<input v-model="aliasForm.effective_to" data-testid="alias-to" type="date" /></label>
          <button class="btn primary" data-testid="alias-submit" type="submit">登记别名</button>
        </form>
      </article>
    </div>

    <!-- 2. 查询 -->
    <article class="fi-card">
      <h3>按表与生效日期查询字段身份</h3>
      <form class="fi-query" data-testid="query-form" @submit.prevent="runQuery">
        <label>表名（可空）<input v-model="queryForm.table" data-testid="query-table" placeholder="留空则跨表检索" /></label>
        <label>字段名 / 别名<input v-model="queryForm.name" data-testid="query-name" placeholder="order_code" /></label>
        <label>生效日期<input v-model="queryForm.effective_date" data-testid="query-date" type="date" required /></label>
        <button class="btn primary" data-testid="query-submit" type="submit">查询</button>
        <button class="btn ghost" data-testid="query-clear" type="button" @click="clearQuery">重置条件</button>
      </form>

      <p v-if="queryMessage" class="fi-message" data-testid="query-message">{{ queryMessage }}</p>

      <div v-if="identity" class="fi-result">
        <div class="fi-badge" :data-state="identity.resolved ? 'resolved' : identity.ambiguous ? 'ambiguous' : 'missing'" data-testid="identity-state">
          {{ identity.resolved ? '已核实' : identity.ambiguous ? '同名候选，未猜来源' : '未找到可确认身份' }}
        </div>
        <table class="data-table">
          <tbody>
            <tr><th>生产表</th><td data-testid="production-table">{{ identity.production_table || '—' }}</td></tr>
            <tr><th>生产列</th><td data-testid="production-column">{{ identity.production_column || '—' }}</td></tr>
            <tr>
              <th>候选</th>
              <td data-testid="candidate-list">
                <span v-if="!identity.candidates.length">—</span>
                <span v-for="candidate in identity.candidates" :key="`${candidate.table}.${candidate.column}`" class="fi-chip">
                  {{ candidate.table }}.{{ candidate.column }}
                  <em v-if="candidate.aliases.length">（别名 {{ candidate.aliases.join('/') }}）</em>
                </span>
              </td>
            </tr>
            <tr>
              <th>读取路径</th>
              <td data-testid="read-paths">
                <span v-if="!identity.read_paths.length">—</span>
                <span v-for="path in identity.read_paths" :key="path.edge_id" class="fi-chip">
                  {{ path.source }} → {{ path.target }}<em>（{{ path.transform }}）</em>
                </span>
              </td>
            </tr>
            <tr>
              <th>下游表</th>
              <td data-testid="downstream-targets">{{ identity.impact.downstream_targets.join('、') || '—' }}</td>
            </tr>
            <tr>
              <th>影响字段</th>
              <td data-testid="impact-item-count">{{ identity.impact.total_impact_items }}</td>
            </tr>
          </tbody>
        </table>

        <div class="page-actions">
          <button class="btn primary" data-testid="generate-entries" type="button" @click="generateEntries">
            按字段影响生成评估条目
          </button>
        </div>
      </div>
    </article>

    <!-- 3. 影响单 -->
    <article v-if="batch" class="fi-card">
      <h3>放入影响单的字段与写入任务</h3>
      <p class="fi-hint" data-testid="batch-summary">
        可提交 {{ batch.submittable_count }} 条，尚欠证据 {{ batch.blocked_count }} 条
      </p>
      <table class="data-table">
        <thead>
          <tr><th>条目</th><th>字段</th><th>生产表</th><th>目标表</th><th>写入任务</th><th>状态</th><th>缺失证据</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in batch.entries" :key="entry.id" :data-testid="`entry-${entry.id}`">
            <td>{{ entry.id }}</td>
            <td>{{ entry.field || '—' }}</td>
            <td>{{ entry.production_table || '—' }}</td>
            <td>{{ entry.target_table || '—' }}</td>
            <td>{{ entry.task_name }}</td>
            <td>{{ entry.status }}</td>
            <td>{{ entry.缺失证据.join('、') || '—' }}</td>
            <td>
              <button
                v-if="entry.可提交"
                class="link"
                :data-testid="`confirm-${entry.id}`"
                type="button"
                @click="confirm(entry.id)"
              >
                确认条目
              </button>
              <span v-else class="fi-blocked">证据不全，不可提交</span>
            </td>
          </tr>
        </tbody>
      </table>
      <div class="page-actions">
        <button class="btn" data-testid="run-drill" type="button" @click="drill">
          用已确认条目执行候选列演练
        </button>
      </div>
      <p v-if="drillMessage" class="fi-message" data-testid="drill-message">{{ drillMessage }}</p>
    </article>

    <!-- 4. 别名修订 -->
    <article class="fi-card">
      <h3>别名有效期与修订</h3>
      <table class="data-table">
        <thead>
          <tr><th>ID</th><th>表</th><th>别名</th><th>目标列</th><th>生效期</th><th>版本</th><th>修订</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in aliases" :key="row.id" :data-testid="`alias-row-${row.id}`">
            <td>{{ row.id }}</td>
            <td>{{ row.table }}</td>
            <td>{{ row.alias }}</td>
            <td>{{ row.column }}</td>
            <td>{{ row.effective_from }} ~ {{ row.effective_to || '至今' }}</td>
            <td>v{{ row.revision }}</td>
            <td>
              <input v-model="revisionDates[row.id]" :data-testid="`revision-date-${row.id}`" type="date" />
              <input v-model="revisionColumns[row.id]" :data-testid="`revision-column-${row.id}`" placeholder="新列名（可空）" />
              <button class="link" :data-testid="`revision-submit-${row.id}`" type="button" @click="revise(row.id)">
                修订口径
              </button>
            </td>
          </tr>
          <tr v-if="!aliases.length">
            <td colspan="7" class="empty-state">暂无别名登记</td>
          </tr>
        </tbody>
      </table>
      <p v-if="revisionMessage" class="fi-message" data-testid="revision-message">{{ revisionMessage }}</p>
      <p v-if="recalculation" class="fi-hint" data-testid="recalculation">
        重算受影响写入目标：{{ recalculation.affected_write_targets.join('、') || '—' }}；
        旧日期 {{ recalculation.history_snapshot.query_date }} 仍可回查
        {{ recalculation.history_snapshot.table }}.{{ recalculation.history_snapshot.column }}
      </p>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  aliasHistory,
  confirmEntry,
  generateImpactEntries,
  registerAlias,
  registerField,
  resolveIdentity,
  reviseAlias,
  runDrill,
  type AliasRecord,
  type ImpactBatch,
  type IdentityResult,
  type Recalculation,
} from '@/api/field-identity'

const today = new Date().toISOString().slice(0, 10)

const fieldForm = ref({ table: '', column: '', effective_from: '', effective_to: '' })
const aliasForm = ref({ table: '', alias: '', column: '', effective_from: '', effective_to: '' })
const queryForm = ref({ table: '', name: '', effective_date: today })

const identity = ref<IdentityResult | null>(null)
const queryMessage = ref('')
const batch = ref<ImpactBatch | null>(null)
const drillMessage = ref('')
const aliases = ref<AliasRecord[]>([])
const revisionDates = ref<Record<number, string>>({})
const revisionColumns = ref<Record<number, string>>({})
const revisionMessage = ref('')
const recalculation = ref<Recalculation | null>(null)

async function loadAliases() {
  try {
    const payload = await aliasHistory(queryForm.value.table || undefined)
    aliases.value = payload.items
  } catch (error) {
    revisionMessage.value = error instanceof Error ? error.message : '别名历史读取失败'
  }
}

async function submitField() {
  queryMessage.value = ''
  try {
    const payload = await registerField({ ...fieldForm.value })
    queryMessage.value = payload.message
    await loadAliases()
  } catch (error) {
    queryMessage.value = error instanceof Error ? error.message : '登记列失败'
  }
}

async function submitAlias() {
  queryMessage.value = ''
  try {
    const payload = await registerAlias({ ...aliasForm.value })
    queryMessage.value = payload.message
    await loadAliases()
  } catch (error) {
    queryMessage.value = error instanceof Error ? error.message : '登记别名失败'
  }
}

function clearQuery() {
  queryForm.value = { table: '', name: '', effective_date: today }
  identity.value = null
  batch.value = null
  queryMessage.value = ''
  drillMessage.value = ''
}

async function runQuery() {
  queryMessage.value = ''
  drillMessage.value = ''
  batch.value = null
  try {
    identity.value = await resolveIdentity({
      table: queryForm.value.table || undefined,
      name: queryForm.value.name || undefined,
      effective_date: queryForm.value.effective_date,
    })
    queryMessage.value = identity.value.message
  } catch (error) {
    identity.value = null
    queryMessage.value = error instanceof Error ? error.message : '查询失败'
  }
}

async function generateEntries() {
  queryMessage.value = ''
  try {
    batch.value = await generateImpactEntries({
      table: queryForm.value.table || undefined,
      column: queryForm.value.name || undefined,
      effective_date: queryForm.value.effective_date || undefined,
      change_type: 'MODIFY_TYPE',
    })
    queryMessage.value = batch.value.message
  } catch (error) {
    queryMessage.value = error instanceof Error ? error.message : '生成影响单失败'
  }
}

async function confirm(entryId: number) {
  drillMessage.value = ''
  try {
    const payload = await confirmEntry(entryId)
    drillMessage.value = payload.message
    if (batch.value) {
      batch.value = {
        ...batch.value,
        entries: batch.value.entries.map((entry) =>
          entry.id === entryId ? { ...entry, status: '已确认' } : entry,
        ),
      }
    }
  } catch (error) {
    drillMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function drill() {
  drillMessage.value = ''
  if (!batch.value) return
  const confirmed = batch.value.entries.filter((entry) => entry.status === '已确认')
  try {
    const payload = await runDrill(confirmed.map((entry) => entry.id))
    drillMessage.value = `${payload.message}；写入结果：${payload.write_results
      .map((item) => item.result)
      .join('、')}`
  } catch (error) {
    drillMessage.value = error instanceof Error ? error.message : '演练失败'
  }
}

async function revise(aliasId: number) {
  revisionMessage.value = ''
  recalculation.value = null
  const effectiveFrom = revisionDates.value[aliasId]
  if (!effectiveFrom) {
    revisionMessage.value = '请先选择修订生效日期'
    return
  }
  try {
    const payload = await reviseAlias(aliasId, {
      effective_from: effectiveFrom,
      column: revisionColumns.value[aliasId] || '',
      note: '页面修订别名口径',
    })
    revisionMessage.value = payload.message
    recalculation.value = payload.recalculation
    await loadAliases()
  } catch (error) {
    revisionMessage.value = error instanceof Error ? error.message : '修订失败'
  }
}

function emptyBatch(): ImpactBatch {
  return {
    query: { table: '', effective_date: '', name: '' },
    resolved: false,
    ambiguous: false,
    production_table: null,
    production_column: null,
    message: '',
    entries: [],
    submittable_count: 0,
    blocked_count: 0,
  }
}

onMounted(loadAliases)
</script>

<style scoped>
.fi-grid { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.fi-card { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; flex: 1; min-width: 320px; }
.fi-card h3 { margin: 0 0 6px; font-size: 14px; }
.fi-hint { color: var(--muted); font-size: 12px; margin: 0 0 10px; }
.fi-card form label { display: block; margin-bottom: 8px; font-size: 12px; color: var(--muted); }
.fi-card form input { width: 100%; box-sizing: border-box; margin-top: 4px; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.fi-query { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; }
.fi-query label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.fi-query input { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.fi-message { font-size: 13px; color: #1f6feb; background: #eef4ff; padding: 8px 10px; border-radius: 6px; margin: 10px 0; }
.fi-badge { display: inline-block; font-size: 12px; padding: 3px 10px; border-radius: 10px; margin: 8px 0; background: #e2e8f0; color: #1f2937; }
.fi-badge[data-state="resolved"] { background: #dcfce7; color: #166534; }
.fi-badge[data-state="ambiguous"] { background: #fef3c7; color: #92400e; }
.fi-badge[data-state="missing"] { background: #fee2e2; color: #991b1b; }
.fi-chip { display: inline-block; margin: 0 6px 4px 0; padding: 2px 8px; border-radius: 4px; background: #f1f5f9; font-size: 12px; }
.fi-chip em { color: var(--muted); font-style: normal; }
.fi-blocked { color: #b42318; font-size: 12px; }
</style>

<template>
  <div class="knowledge-manager">
    <header class="page-heading">
      <div>
        <h1>{{ t('knowledge.title') }}</h1>
        <p>{{ t('knowledge.subtitle') }}</p>
      </div>
      <el-button :loading="loading" @click="refreshAll">{{ t('common.refresh') }}</el-button>
    </header>

    <div class="market-entry">
      <el-alert :title="t('knowledge.marketConnected')" type="info" :closable="false" show-icon />
      <el-tag v-if="marketAuth.authenticated" type="success" effect="plain">
        {{ t('market.accountConnected', { name: marketAuthDisplayName }) }}
      </el-tag>
      <span v-else class="market-login-hint">{{ t('knowledge.loginRequired') }}</span>
      <el-button
        type="primary"
        plain
        :disabled="!marketAuth.authenticated"
        :loading="marketOpening"
        @click="openKnowledgeMarket"
      >
        {{ t('knowledge.openMarket') }}
      </el-button>
    </div>

    <el-tabs v-model="activeTab" class="knowledge-tabs">
      <el-tab-pane :label="t('knowledge.overview')" name="overview">
        <div class="status-grid" v-loading="loading">
          <el-card v-if="status" class="status-card" shadow="never">
            <template #header>
              <div class="card-heading">
                <strong>{{ status.name }}</strong>
                <el-tag :type="status.status === 'ready' ? 'success' : 'danger'">
                  {{ status.status === 'ready' ? t('knowledge.ready') : t('knowledge.degraded') }}
                </el-tag>
              </div>
            </template>
            <dl>
              <div><dt>{{ t('knowledge.entries') }}</dt><dd>{{ status.entries ?? 0 }}</dd></div>
              <div><dt>{{ t('knowledge.disabled') }}</dt><dd>{{ status.disabled_entries ?? 0 }}</dd></div>
              <div><dt>{{ t('knowledge.packs') }}</dt><dd>{{ status.packs ?? 0 }}</dd></div>
            </dl>
            <div class="source-list">
              <el-tag v-for="source in status.sources || []" :key="source.tag" size="small" effect="plain">
                {{ source.tag }} · {{ source.entries }}
              </el-tag>
            </div>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="t('knowledge.catalog')" name="catalog">
        <div class="toolbar">
          <el-input v-model="query" clearable :placeholder="t('knowledge.searchPlaceholder')" @keyup.enter="loadEntries(true)" />
          <el-button type="primary" @click="loadEntries(true)">{{ t('common.search') }}</el-button>
        </div>
        <div class="table-shell">
          <el-table :data="entries" v-loading="entriesLoading" :row-key="knowledgeEntryRowKey">
            <el-table-column prop="title" :label="t('knowledge.term')" min-width="180" />
            <el-table-column prop="summary" :label="t('knowledge.summary')" min-width="320" show-overflow-tooltip />
            <el-table-column :label="t('knowledge.source')" width="170">
              <template #default="scope">{{ scope.row.source?.name }}</template>
            </el-table-column>
            <el-table-column :label="t('knowledge.actions')" width="190">
              <template #default="scope">
                <el-button link type="primary" @click="openEntry(scope.row)">{{ t('knowledge.details') }}</el-button>
                <el-button link :type="scope.row.disabled ? 'success' : 'danger'" @click="toggleEntry(scope.row)">
                  {{ scope.row.disabled ? t('knowledge.restore') : t('knowledge.disable') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
        <div class="pager">
          <el-button :disabled="offset === 0" @click="previousPage">{{ t('knowledge.previous') }}</el-button>
          <span>{{ offset + 1 }}–{{ offset + entries.length }}</span>
          <el-button :disabled="!hasMore" @click="nextPage">{{ t('knowledge.next') }}</el-button>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="t('knowledge.packs')" name="packs">
        <div class="toolbar">
          <input ref="fileInput" type="file" accept="application/json,.json" hidden @change="importSelectedPack" />
          <el-button type="primary" @click="fileInput?.click()">{{ t('knowledge.importPack') }}</el-button>
        </div>
        <div class="table-shell">
          <el-table :data="packs" v-loading="packsLoading">
            <el-table-column prop="pack_id" :label="t('knowledge.packId')" min-width="180" />
            <el-table-column :label="t('knowledge.materialType')" width="150">
              <template #default="scope">
                <el-select
                  :model-value="scope.row.effective_material_type || 'knowledge'"
                  @change="setPackMaterialType(scope.row, String($event))"
                >
                  <el-option label="knowledge" value="knowledge" />
                  <el-option label="corpus" value="corpus" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column prop="entries" :label="t('knowledge.entries')" width="100" />
            <el-table-column :label="t('knowledge.subscription')" min-width="200">
              <template #default="scope">
                {{ scope.row.subscription ? `${scope.row.subscription.provider} · ${scope.row.subscription.version}` : t('knowledge.localImport') }}
              </template>
            </el-table-column>
            <el-table-column :label="t('knowledge.indexStatus')" min-width="280">
              <template #default="scope">
                <dl class="index-status-list">
                  <div><dt>{{ t('knowledge.indexOrigin') }}</dt><dd>{{ displayIndexValue(scope.row.index_origin) }}</dd></div>
                  <div><dt>{{ t('knowledge.indexTrust') }}</dt><dd>{{ displayIndexValue(scope.row.index_trust) }}</dd></div>
                  <div><dt>{{ t('knowledge.indexValidation') }}</dt><dd>{{ displayIndexValue(scope.row.index_validation) }}</dd></div>
                  <div><dt>{{ t('knowledge.indexFallback') }}</dt><dd>{{ displayIndexValue(scope.row.index_fallback_reason) }}</dd></div>
                  <div>
                    <dt>{{ t('knowledge.localEmbeddingState') }}</dt>
                    <dd>{{ scope.row.local_embedding_enabled ? t('knowledge.enabled') : t('knowledge.disabledState') }}</dd>
                  </div>
                </dl>
              </template>
            </el-table-column>
            <el-table-column :label="t('knowledge.autoContext')" width="130">
              <template #default="scope">
                <el-switch
                  :model-value="scope.row.auto_context === true"
                  @change="setPackAuto(scope.row, Boolean($event))"
                />
              </template>
            </el-table-column>
            <el-table-column :label="t('knowledge.allowLocalEmbedding')" min-width="170">
              <template #default="scope">
                <el-tooltip :content="t('knowledge.indexPolicyHint')" placement="top">
                  <el-switch
                    :model-value="scope.row.local_embedding_enabled === true"
                    :aria-label="t('knowledge.allowLocalEmbedding')"
                    @change="setPackIndexPolicy(scope.row, Boolean($event))"
                  />
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column :label="t('knowledge.actions')" width="110">
              <template #default="scope">
                <el-button link type="danger" @click="removePack(scope.row)">{{ t('common.delete') }}</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="t('knowledge.diagnostics')" name="diagnostics">
        <div class="table-shell">
          <el-table :data="diagnostics" v-loading="diagnosticsLoading">
            <el-table-column prop="timestamp" :label="t('knowledge.time')" width="210" />
            <el-table-column prop="entry_title" :label="t('knowledge.term')" min-width="180" />
            <el-table-column prop="match_mode" :label="t('knowledge.matchMode')" width="140" />
            <el-table-column :label="t('knowledge.delivered')" width="100">
              <template #default="scope">
                <el-tag :type="scope.row.card_delivered ? 'success' : 'info'">
                  {{ scope.row.card_delivered ? t('knowledge.yes') : t('knowledge.no') }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>

    <el-drawer v-model="drawerOpen" :title="selectedEntry?.title || ''" size="520px">
      <template v-if="selectedEntry">
        <h3>{{ t('knowledge.summary') }}</h3><p>{{ selectedEntry.summary }}</p>
        <h3>{{ t('knowledge.terms') }}</h3><pre>{{ JSON.stringify(selectedEntry.terms, null, 2) }}</pre>
        <h3>{{ t('knowledge.tags') }}</h3><p>{{ selectedEntry.tags.join(' · ') }}</p>
        <h3>{{ t('knowledge.content') }}</h3><pre>{{ selectedEntry.content }}</pre>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useI18n } from 'vue-i18n'
import { knowledgeApi, type KnowledgeStatus, type KnowledgeEntrySummary, type KnowledgePackSummary } from '@/api/knowledge'
import { getMarketUrl } from '@/api/market'
import { useMarketAuth } from '@/composables/useMarketAuth'
import { openExternalUrl } from '@/utils/openExternal'

const { t } = useI18n()
const {
  marketAuth,
  marketAuthDisplayName,
  loadMarketAuthStatus,
} = useMarketAuth()
const activeTab = ref('overview')
const loading = ref(false)
const status = ref<(KnowledgeStatus & { status: 'ready' | 'degraded' }) | null>(null)
const query = ref('')
const entries = ref<KnowledgeEntrySummary[]>([])
const entriesLoading = ref(false)
const offset = ref(0)
const pageSize = 50
const hasMore = ref(false)
const drawerOpen = ref(false)
const selectedEntry = ref<KnowledgeEntrySummary | null>(null)
const packs = ref<KnowledgePackSummary[]>([])
const packsLoading = ref(false)
const diagnostics = ref<any[]>([])
const diagnosticsLoading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const marketOpening = ref(false)

function knowledgeEntryRowKey(row: KnowledgeEntrySummary): string {
  return JSON.stringify([
    row.source?.tag || '',
    row.title,
  ])
}

async function openKnowledgeMarket() {
  if (!marketAuth.value.authenticated) {
    ElMessage.warning(t('knowledge.loginRequired'))
    return
  }
  marketOpening.value = true
  try {
    const base = await getMarketUrl()
    if (!base) throw new Error(t('knowledge.marketUnavailable'))
    const response = await fetch('/market/pair-code', { method: 'POST' })
    if (!response.ok) throw new Error(t('knowledge.marketPairFailed'))
    const pairing = await response.json()
    const code = String(pairing.one_time_code || '')
    const port = Number(pairing.port)
    if (!code || !Number.isInteger(port)) throw new Error(t('knowledge.marketPairFailed'))
    const query = new URLSearchParams({
      neko_pair: code,
      neko_port: String(port),
    })
    openExternalUrl(`${base.replace(/\/+$/, '')}/#/knowledge?${query}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('knowledge.marketPairFailed'))
  } finally {
    marketOpening.value = false
  }
}

async function refreshAll() {
  loading.value = true
  try {
    const response = await knowledgeApi.status()
    status.value = response.status || null
  } catch {
    ElMessage.error(t('knowledge.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function loadEntries(reset = false) {
  if (reset) offset.value = 0
  entriesLoading.value = true
  try {
    const response = await knowledgeApi.entries({ query: query.value, limit: pageSize, offset: offset.value })
    entries.value = response.items || []
    hasMore.value = Boolean(response.has_more)
  } catch { ElMessage.error(t('knowledge.loadFailed')) }
  finally { entriesLoading.value = false }
}

async function openEntry(row: KnowledgeEntrySummary) {
  const response = await knowledgeApi.entry({ source: row.source.tag, title: row.title })
  selectedEntry.value = response.entry || null
  drawerOpen.value = Boolean(selectedEntry.value)
}

async function toggleEntry(row: KnowledgeEntrySummary) {
  try {
    await knowledgeApi.setEntryDisabled({ source: row.source.tag, title: row.title, disabled: !row.disabled })
    row.disabled = !row.disabled
  } catch { ElMessage.error(t('knowledge.operationFailed')) }
}

function previousPage() { offset.value = Math.max(0, offset.value - pageSize); loadEntries() }
function nextPage() { offset.value += pageSize; loadEntries() }

async function loadPacks() {
  packsLoading.value = true
  try { packs.value = (await knowledgeApi.packs()).packs || [] }
  catch { ElMessage.error(t('knowledge.loadFailed')) }
  finally { packsLoading.value = false }
}

async function importSelectedPack(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    const pack = JSON.parse(await file.text())
    const response = await knowledgeApi.importPack(pack)
    ElMessage.info(
      response.state === 'queued'
        ? t('knowledge.importQueued')
        : t('knowledge.importSuccess'),
    )
    await Promise.all([refreshAll(), loadPacks()])
  } catch { ElMessage.error(t('knowledge.invalidPack')) }
}

async function setPackAuto(row: KnowledgePackSummary, enabled: boolean) {
  try {
    await knowledgeApi.setPackAutoContext({ pack_id: row.pack_id, enabled })
    row.auto_context = enabled
  } catch { ElMessage.error(t('knowledge.operationFailed')) }
}

async function setPackMaterialType(row: KnowledgePackSummary, materialType: string) {
  try {
    await knowledgeApi.setPackMaterialType({ pack_id: row.pack_id, material_type: materialType })
    row.effective_material_type = materialType as 'knowledge' | 'corpus'
    if (materialType === 'corpus') row.auto_context = true
    await refreshAll()
  } catch { ElMessage.error(t('knowledge.operationFailed')) }
}

function displayIndexValue(value: unknown): string {
  const text = String(value ?? '').trim()
  return text || t('common.nA')
}

async function setPackIndexPolicy(row: KnowledgePackSummary, enabled: boolean) {
  try {
    await knowledgeApi.setPackIndexPolicy({
      pack_id: row.pack_id,
      local_embedding_enabled: enabled,
    })
    row.local_embedding_enabled = enabled
  } catch { ElMessage.error(t('knowledge.operationFailed')) }
}

async function removePack(row: any) {
  try {
    await ElMessageBox.confirm(t('knowledge.removeConfirm', { name: row.pack_id }), t('common.warning'), { type: 'warning' })
    await knowledgeApi.removePack({ pack_id: row.pack_id })
    await Promise.all([refreshAll(), loadPacks()])
  } catch (error: any) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(t('knowledge.operationFailed'))
  }
}

async function loadDiagnostics() {
  diagnosticsLoading.value = true
  try { diagnostics.value = (await knowledgeApi.diagnostics()).items || [] }
  catch { ElMessage.error(t('knowledge.loadFailed')) }
  finally { diagnosticsLoading.value = false }
}

watch(activeTab, (tab) => {
  if (tab === 'catalog') loadEntries(true)
  if (tab === 'packs') loadPacks()
  if (tab === 'diagnostics') loadDiagnostics()
})

onMounted(() => {
  void Promise.all([refreshAll(), loadMarketAuthStatus()])
})
</script>

<style scoped>
.knowledge-manager {
  --knowledge-surface: var(--el-bg-color);
  --knowledge-surface-muted: var(--el-fill-color-extra-light);
  --knowledge-line: var(--el-border-color-lighter);
  --knowledge-accent-soft: var(--el-color-primary-light-9);
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 18px;
  width: 100%;
  min-width: 0;
}

.market-entry {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 12px;
  border: 1px solid var(--knowledge-line);
  border-radius: 10px;
  background: var(--knowledge-surface);
  min-width: 0;
  box-shadow: 0 8px 22px rgba(15, 23, 42, 0.03);
}

.market-entry .el-alert {
  flex: 1 1 320px;
  min-width: 0;
  padding: 0;
  border: 0;
  background: transparent;
}

.market-entry .el-button {
  margin-left: auto;
}

.market-login-hint {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}

.page-heading,
.card-heading,
.switch-row,
.toolbar,
.pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.page-heading h1 {
  margin: 0 0 6px;
  font-size: 24px;
  line-height: 1.2;
}

.page-heading p {
  margin: 0;
  color: var(--el-text-color-secondary);
}

.knowledge-tabs {
  min-width: 0;
  border: 1px solid var(--knowledge-line);
  border-radius: 10px;
  background: var(--knowledge-surface);
  box-shadow: 0 10px 28px rgba(15, 23, 42, 0.04);
}

.knowledge-tabs :deep(.el-tabs__header) {
  margin: 0;
  padding: 8px;
  border-bottom: 1px solid var(--knowledge-line);
  background: var(--knowledge-surface-muted);
  border-radius: 10px 10px 0 0;
}

.knowledge-tabs :deep(.el-tabs__nav-wrap) {
  min-width: 0;
}

.knowledge-tabs :deep(.el-tabs__nav-wrap::after),
.knowledge-tabs :deep(.el-tabs__active-bar) {
  display: none;
}

.knowledge-tabs :deep(.el-tabs__nav-scroll) {
  overflow-x: auto;
  scrollbar-width: none;
}

.knowledge-tabs :deep(.el-tabs__nav-scroll::-webkit-scrollbar) {
  display: none;
}

.knowledge-tabs :deep(.el-tabs__nav) {
  display: flex;
  flex-wrap: nowrap;
  gap: 4px;
  min-width: max-content;
}

.knowledge-tabs :deep(.el-tabs__item) {
  position: relative;
  height: 36px;
  padding: 0 16px;
  border-radius: 7px;
  color: var(--el-text-color-regular);
  font-size: 14px;
  font-weight: 500;
  transition:
    color 160ms ease,
    background-color 160ms ease,
    box-shadow 160ms ease;
}

.knowledge-tabs :deep(.el-tabs__item:hover) {
  color: var(--el-color-primary);
  background: var(--el-fill-color-light);
}

.knowledge-tabs :deep(.el-tabs__item.is-active) {
  color: var(--el-color-primary);
  background: var(--knowledge-surface);
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.05);
}

.knowledge-tabs :deep(.el-tabs__item.is-active::after) {
  position: absolute;
  right: 12px;
  bottom: 4px;
  left: 12px;
  height: 2px;
  border-radius: 999px;
  background: var(--el-color-primary);
  content: '';
}

.knowledge-tabs :deep(.el-tabs__item.is-focus) {
  box-shadow: inset 0 0 0 1px var(--el-color-primary-light-5);
}

.knowledge-tabs :deep(.el-tabs__item.is-active.is-focus) {
  box-shadow:
    0 1px 2px rgba(15, 23, 42, 0.05),
    0 0 0 3px rgba(64, 158, 255, 0.12);
}

.knowledge-tabs :deep(.el-tabs__content) {
  padding: 18px;
  min-width: 0;
}

.status-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
  gap: 16px;
}

.status-card {
  border-color: transparent;
  border-radius: 10px;
  box-shadow: none;
}

.status-card :deep(.el-card__header) {
  padding: 16px 18px;
  border-bottom-color: var(--knowledge-line);
}

.status-card :deep(.el-card__body) {
  padding: 18px;
}

dl {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}

dl div {
  padding: 12px;
  border: 0;
  border-radius: 8px;
  background: var(--knowledge-surface-muted);
}

dt {
  color: var(--el-text-color-secondary);
  font-size: 12px;
}

dd {
  margin: 4px 0 0;
  font-size: 20px;
  font-weight: 700;
  line-height: 1.15;
}

.switch-row {
  margin-top: 14px;
}

.source-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
}

.source-list .el-tag {
  max-width: min(260px, 100%);
}

.source-list :deep(.el-tag__content) {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.toolbar {
  justify-content: flex-start;
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--knowledge-line);
  border-radius: 8px;
  background: var(--knowledge-surface-muted);
}

.toolbar .el-select {
  width: min(210px, 100%);
}

.toolbar .el-input {
  flex: 1 1 320px;
  min-width: 0;
  max-width: 520px;
}

.table-shell {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--knowledge-line);
  border-radius: 10px;
  background: var(--knowledge-surface);
}

.table-shell :deep(.el-table) {
  --el-table-border-color: var(--knowledge-line);
  --el-table-header-bg-color: var(--knowledge-surface-muted);
  --el-table-row-hover-bg-color: var(--el-color-primary-light-9);
}

.table-shell :deep(.el-table__inner-wrapper::before) {
  display: none;
}

.table-shell :deep(.el-table th.el-table__cell) {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
}

.table-shell :deep(.el-table td.el-table__cell) {
  color: var(--el-text-color-regular);
}

.pager {
  justify-content: flex-end;
  margin-top: 14px;
}

.pager span {
  min-width: 70px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
  text-align: center;
}

.index-status-list {
  display: grid;
  grid-template-columns: 1fr;
  gap: 4px;
  margin: 0;
}

.index-status-list div {
  display: grid;
  grid-template-columns: minmax(90px, auto) minmax(0, 1fr);
  gap: 8px;
  padding: 0;
  border: 0;
  background: transparent;
}

.index-status-list dt {
  font-size: 12px;
}

.index-status-list dd {
  min-width: 0;
  margin: 0;
  font-size: 12px;
  font-weight: 500;
  overflow-wrap: anywhere;
}

pre {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  padding: 12px;
  border: 1px solid var(--knowledge-line);
  border-radius: 8px;
  background: var(--knowledge-surface-muted);
}

@media (max-width: 640px) {
  .knowledge-manager {
    padding: 16px;
  }

  .market-entry .el-alert,
  .market-entry .el-button {
    flex-basis: 100%;
    width: 100%;
    margin-left: 0;
  }

  .knowledge-tabs :deep(.el-tabs__header) {
    padding: 6px;
  }

  .knowledge-tabs :deep(.el-tabs__item) {
    height: 34px;
    padding: 0 12px;
  }

  .knowledge-tabs :deep(.el-tabs__content) {
    padding: 14px;
  }

  dl {
    grid-template-columns: 1fr;
  }

  .toolbar .el-input,
  .toolbar .el-select,
  .toolbar .el-button {
    width: 100%;
    max-width: none;
  }

  .pager {
    justify-content: center;
  }
}
</style>

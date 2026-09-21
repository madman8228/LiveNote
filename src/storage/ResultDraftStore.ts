import { deleteRecord, getRecord, putRecord, STORE_NAMES } from './db'
import type { ResultDraftRecord } from './types'

export const ResultDraftStore = {
  get(sessionId: string): Promise<ResultDraftRecord | undefined> {
    return getRecord<ResultDraftRecord>(STORE_NAMES.resultDrafts, sessionId)
  },
  put(draft: ResultDraftRecord): Promise<void> {
    return putRecord(STORE_NAMES.resultDrafts, draft)
  },
  delete(sessionId: string): Promise<void> {
    return deleteRecord(STORE_NAMES.resultDrafts, sessionId)
  },
}

import { deleteRecord, getAllRecords, getRecord, putRecord, STORE_NAMES } from './db'
import type { SessionRecord, SessionStatus } from './types'

export const SessionStore = {
  get(id: string) {
    return getRecord<SessionRecord>(STORE_NAMES.sessions, id)
  },
  list() {
    return getAllRecords<SessionRecord>(STORE_NAMES.sessions).then((sessions) => (
      sessions.sort((a, b) => b.updatedAt - a.updatedAt)
    ))
  },
  listOpen() {
    return getAllRecords<SessionRecord>(STORE_NAMES.sessions).then((sessions) => (
      sessions
        .filter((session) => session.status === 'RECORDING' || session.status === 'PAUSED' || session.status === 'FINALIZING' || session.status === 'INTERRUPTED')
        .sort((a, b) => b.updatedAt - a.updatedAt)
    ))
  },
  put(session: SessionRecord) {
    return putRecord(STORE_NAMES.sessions, session)
  },
  delete(id: string) {
    return deleteRecord(STORE_NAMES.sessions, id)
  },
  async updateStatus(id: string, status: SessionStatus, endedAt: number | null = null): Promise<SessionRecord> {
    const session = await getRecord<SessionRecord>(STORE_NAMES.sessions, id)
    if (!session) throw new Error(`Session 不存在：${id}`)
    const updated = { ...session, status, endedAt, updatedAt: Date.now() }
    await putRecord(STORE_NAMES.sessions, updated)
    return updated
  },
}

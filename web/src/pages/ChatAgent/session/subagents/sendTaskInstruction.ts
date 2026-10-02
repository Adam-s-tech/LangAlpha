/**
 * Sends an instruction the user typed to a running subagent. It shows at once
 * as a pending bubble in the transcript the task's stream writes, and the
 * stream settles it: delivered before the subagent's next model call, or
 * returned when the run ends first. A send that fails never reaches the
 * queue, so nothing on the stream will settle it, and the failure takes the
 * bubble back itself.
 */
import { apiErrorStatus, sendSubagentMessage } from '../../utils/api';
import { taskIdFromAgentId } from '../../utils/agentId';
import type { SubagentRuntime } from '../runtime';
import { addPendingTaskInstruction, hasPendingInstruction, returnTaskInstruction } from './liveEventHandlers';

export async function sendTaskInstruction(
  rt: Pick<SubagentRuntime, 't' | 'updateSubagentCard' | 'subagentStateRefsRef'>,
  threadId: string,
  agentId: string,
  content: string,
): Promise<void> {
  const taskId = taskIdFromAgentId(agentId);
  if (!taskId) return;
  addPendingTaskInstruction({
    taskId: agentId,
    content,
    subagentStateRefs: rt.subagentStateRefsRef.current,
    updateSubagentCard: rt.updateSubagentCard,
  });
  try {
    await sendSubagentMessage(threadId, taskId, content);
  } catch (err) {
    console.error('[sendTaskInstruction] Failed to send subagent instruction:', err);
    // Read after the await, and only while the bubble is still pending: a
    // thread opened since holds other transcripts, and the stream may have
    // returned the instruction first.
    const taskRefs = rt.subagentStateRefsRef.current[agentId];
    if (!rt.updateSubagentCard || !taskRefs || !hasPendingInstruction(taskRefs, content)) return;
    // A 409 is the task settling before the instruction got in, so it reads as
    // a return; any other failure says only that it was not sent.
    const notice = apiErrorStatus(err) === 409
      ? 'chat.taskSteeringReturnedNotification'
      : 'chat.taskSteeringNotSentNotification';
    returnTaskInstruction(taskRefs, content, rt.t(notice));
    rt.updateSubagentCard(agentId, { messages: taskRefs.messages });
  }
}

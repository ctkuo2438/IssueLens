/**
 * The API's data, named as the app uses it. Every type comes from app/openapi.ts,
 * which is generated from the backend's own schema, so a backend change that breaks
 * the frontend fails type checking. scripts/export_openapi.py explains how to
 * regenerate it.
 */
import type { components } from "./openapi";

type Schemas = components["schemas"];

export type Job = Schemas["JobSnapshot"];
export type JobStatus = Job["status"];
export type Progress = Schemas["ProgressEvent"];
export type Source = Schemas["Source"];
export type Finding = Schemas["Finding"];
export type Content = Schemas["AnalysisContent"];
export type IssueCandidate = Schemas["IssueCandidate"];
export type IssueComparison = Schemas["IssueComparison"];
export type IssueSearch = Schemas["IssueSearch"];
export type IssueTemplate = Schemas["IssueTemplate"];
export type LabelRecommendation = Schemas["LabelRecommendation"];
export type RepositoryLabel = Schemas["RepositoryLabel"];
export type Health = Schemas["Health"];
export type AuthSession = Schemas["AuthSession"];

// The same split as TERMINAL_STATUSES in backend/app/models.py. A status the backend
// adds fails type checking here until it is classified as final or not.
const FINAL: Record<JobStatus, boolean> = {
  queued: false,
  running: false,
  completed: true,
  partial: true,
  failed: true,
  cancelled: true,
};

/** Whether the analysis has ended, so no further events will arrive. */
export function isFinished(status: JobStatus): boolean {
  return FINAL[status];
}

import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

/**
 * The Business Functions module's API surface and its types.
 *
 * Its own service rather than another section of `CompassApiService`, for the
 * reason Estimate's is its own: the module has a flag, and a module that can
 * be switched off should take its whole surface with it. The shared service is
 * where a call belongs when two sections both make it; nothing else calls
 * these.
 *
 * The shapes below mirror what `compass/businessfunctions/routes.py` returns,
 * and they are deliberately not clever. A figure's `tone` is a word rather
 * than a colour because the server decides what a number *means* and this
 * side decides how to show it; the same split that lets a handler say "warn"
 * without knowing anything about CSS.
 */

export interface FunctionCard {
  id: string;
  name: string;
  subtitle: string;
  features: number;
  empty: boolean;
}

export interface ActionSpec {
  id: string;
  label: string;
  confirm: string;
  reversible: boolean;
  /** Reaches past Compass — notifies somebody, books days. Taken one at a time. */
  outward: boolean;
  /** When set, what to ask the person before doing it. An action with a
   *  reason prompt is never offered by the assistant: the reason is the
   *  substance of the decision and has to be the reviewer's own. */
  note_label: string;
}

export interface FeatureCard {
  id: string;
  name: string;
  short: string;
  blurb: string;
  scope: string[];
  forms: FormSpec[];
  /** What each scope dimension may be set to. The manifest's, not the UI's:
   *  a quarter means something to a reconciliation and nothing to an annual
   *  limit, so the values travel with the feature that is scoped by them. */
  choices: Record<string, string[]>;
  /** Why the screen is empty until a scope is chosen, in this feature's words. */
  scope_why: string;
  actions: ActionSpec[];
}

export interface QueueItem {
  feature: string;
  feature_name: string;
  count: string;
  what: string;
  key: string;
}

export interface RailView {
  title: string;
  subtitle: string;
  scope_label: string;
  starters: string[];
  can_act: boolean;
}

export interface FunctionDetail {
  id: string;
  name: string;
  subtitle: string;
  blurb: string;
  empty: boolean;
  features: FeatureCard[];
  queue: QueueItem[];
  rail: RailView;
}

export interface Figure {
  key: string;
  value: string;
  caption: string;
  /** 'plain' | 'good' | 'warn' | 'bad' — the reading, not the colour. */
  tone: string;
}

export interface TabSpec {
  key: string;
  label: string;
  count: number | null;
}

export interface RuleSpec {
  id: string;
  headline: string;
  detail: string;
  /** 'notice' explains; 'limit' says something cannot be done. */
  tone: string;
}

export interface FormField {
  id: string;
  label: string;
  kind: 'text' | 'money' | 'date' | 'choice';
  help: string;
  required: boolean;
  options: string[];
}

export interface FormSpec {
  id: string;
  label: string;
  blurb: string;
  submit_label: string;
  confirm: string;
  /** Opened from a row rather than from the header, and submitted with its
   *  id — answering a question about something, not recording something new. */
  on_row: boolean;
  fields: FormField[];
}

export interface FeatureView {
  id: string;
  name: string;
  short?: string;
  /** Present instead of everything else when a selector has not been chosen. */
  needs_scope?: string[];
  scope_why?: string;
  /** Which field in a row identifies it. The handler's answer, not a guess. */
  row_key?: string;
  scope?: { entity: string; period: string };
  figures?: Figure[];
  tabs?: TabSpec[];
  tab?: string;
  rows?: Record<string, unknown>[];
  rules?: RuleSpec[];
  actions?: ActionSpec[];
  /** Things a person records that did not exist before. */
  forms?: FormSpec[];
  rail: RailView;
}

export interface PlanTarget {
  id: string;
  label: string;
}

export interface Plan {
  id: string;
  action: string;
  headline: string;
  detail: string;
  targets: PlanTarget[];
  outward: boolean;
  reversible: boolean;
}

/** What `/ask` came back with. Three shapes, rendered three ways. */
export type AskResult =
  | { kind: 'plan'; plan: Plan }
  | { kind: 'clarify'; text: string; options: PlanTarget[] }
  | { kind: 'answer'; text: string; facts: Record<string, unknown> };

export interface ActResult {
  ok: boolean;
  said: string;
  touched?: string[];
}

const ROOT = '/v1/business-functions';

@Injectable({ providedIn: 'root' })
export class BusinessFunctionsApi {
  private readonly http = inject(HttpClient);

  functions(): Promise<{ functions: FunctionCard[] }> {
    return firstValueFrom(this.http.get<{ functions: FunctionCard[] }>(ROOT));
  }

  /** One function's overview. `entity`/`period` only matter for its queue. */
  detail(id: string, scope: { entity?: string; period?: string } = {}):
    Promise<FunctionDetail> {
    return firstValueFrom(
      this.http.get<FunctionDetail>(`${ROOT}/${id}`, { params: clean(scope) }),
    );
  }

  feature(
    fnId: string,
    featureId: string,
    scope: { entity?: string; period?: string; tab?: string } = {},
  ): Promise<FeatureView> {
    return firstValueFrom(
      this.http.get<FeatureView>(
        `${ROOT}/${fnId}/features/${featureId}`, { params: clean(scope) },
      ),
    );
  }

  /** A row button. The click was the confirmation, so there is no plan. */
  act(
    fnId: string,
    featureId: string,
    body: {
      action: string; targets: string[]; note?: string;
      entity?: string; period?: string;
    },
  ): Promise<ActResult> {
    return firstValueFrom(
      this.http.post<ActResult>(`${ROOT}/${fnId}/features/${featureId}/act`, body),
    );
  }

  /** What a filled-in form would do. Changes nothing. */
  previewForm(
    fnId: string,
    featureId: string,
    formId: string,
    body: {
      values: Record<string, string>; about?: string;
      entity?: string; period?: string;
    },
  ): Promise<ActResult> {
    return firstValueFrom(
      this.http.post<ActResult>(
        `${ROOT}/${fnId}/features/${featureId}/forms/${formId}/preview`, body,
      ),
    );
  }

  /** Record it. Against whoever is signed in — the body never says who. */
  submitForm(
    fnId: string,
    featureId: string,
    formId: string,
    body: {
      values: Record<string, string>; about?: string;
      entity?: string; period?: string;
    },
  ): Promise<ActResult> {
    return firstValueFrom(
      this.http.post<ActResult>(
        `${ROOT}/${fnId}/features/${featureId}/forms/${formId}`, body,
      ),
    );
  }

  /** The rail. Changes nothing — what comes back is a plan, an answer or a question. */
  ask(
    fnId: string,
    featureId: string,
    body: { text: string; tab?: string; entity?: string; period?: string },
  ): Promise<AskResult> {
    return firstValueFrom(
      this.http.post<AskResult>(`${ROOT}/${fnId}/features/${featureId}/ask`, body),
    );
  }

  confirmPlan(planId: string, fnId: string, featureId: string): Promise<ActResult> {
    return firstValueFrom(
      this.http.post<ActResult>(`${ROOT}/plans/${planId}/confirm`,
        { function_id: fnId, feature_id: featureId }),
    );
  }

  cancelPlan(planId: string): Promise<ActResult> {
    return firstValueFrom(
      this.http.post<ActResult>(`${ROOT}/plans/${planId}/cancel`, {}),
    );
  }
}

/** Drop empty values so a blank selector is not sent as `entity=`. */
function clean(obj: Record<string, string | undefined>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(obj)) if (v) out[k] = v;
  return out;
}

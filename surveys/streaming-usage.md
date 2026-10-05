---
survey:
  id: streaming-usage
  version: 1
  title: Streaming Services Usage
  settings:
    url_variables:
    - uid
    - rid
    - group
    - lang
    identity:
      respondent: uid
      response: rid
    theme: paper
    default_redirect: default
    ai:
      notice: Some of your responses may be analysed by an AI system to help us understand your answers
        (for example, summarising open-text feedback). Your responses are processed securely, are not
        used to train AI models, and taking part in this analysis is optional.
      consent_required: true
    enrichments:
    - id: recent_stream_search
      event_type: web_search
      window:
        last: 30d
      criteria:
        op: or
        conditions:
        - field: search_term
          operator: contains_any
          value:
          - netflix
          - disney
          - hbo
          - max
          - prime video
          - streaming
          - what to watch
      operation: latest
      field: search_term
      type: scalar
    - id: stream_search_count_30d
      event_type: web_search
      window:
        last: 30d
      criteria:
        op: or
        conditions:
        - field: search_term
          operator: contains_any
          value:
          - netflix
          - disney
          - hbo
          - max
          - prime video
          - streaming
          - what to watch
      operation: count
      field: search_term
      type: number
    - id: ent_app_minutes_7d
      event_type: app_usage
      window:
        last: 7d
      criteria:
        field: app_category
        operator: equals
        value: Entertainment
      operation: sum
      field: minutes
      type: number
    - id: why_sentiment
      source: llm
      opt_in: true
      requires_consent: q_consent
      inputs:
      - q_why_search
      task: Classify the sentiment of the respondent's stated reason as positive, neutral, or negative.
      output:
        type: enum
        values:
        - positive
        - neutral
        - negative
    anti_fraud:
      attention_checks:
      - q_attention
      min_page_seconds: 2
      block_duplicate_uid: true
  hidden:
  - id: stream_score
    calc:
      op: score
      ref:
        kind: question
        id: q_service_use
      map:
        current: 3
        past6: 2
        pastold: 1
        never: 0
  - id: user_segment
    calc:
      op: bucket
      of:
        op: ref
        ref:
          kind: variable
          id: stream_score
      ranges:
      - - 3
        - 3
        - active
      - - 1
        - 2
        - lapsed
      - - 0
        - 0
        - non_user
      default: unknown
  quotas:
  - id: quota_overall
    scope: survey
    limit: 500
  - id: quota_current
    scope: answer
    ref:
      kind: question
      id: q_service_use
    match:
    - current
    limit: 300
  pages:
  - id: p_intro
    questions:
    - id: q_intro_video
      type: video
      title: Please watch this short clip before we begin.
      media:
        url: https://www.youtube.com/watch?v=aqz-KE-bpKQ
        caption: ~30 second intro
    - id: q_consent
      type: radio
      required: true
      title: Welcome {{url:group|there}}! This short study is about video streaming services. Do you consent
        to take part?
      options:
      - value: 'yes'
        label: Yes, I consent
      - value: 'no'
        label: 'No'
  - id: p_screen
    questions:
    - id: q_age
      type: number
      required: true
      title: What is your age?
      validation:
        min: 16
        max: 99
    - id: q_service_use
      type: radio
      required: true
      title: Have you used a paid video streaming service (e.g. Netflix, Disney+, Max)?
      randomization:
        mode: shuffle
        fixed:
        - never
      options:
      - value: current
        label: Currently subscribe
      - value: past6
        label: Subscribed in the last 6 months
      - value: pastold
        label: Subscribed more than 6 months ago
      - value: never
        label: Never subscribed
  - id: p_brands
    display_logic:
      ref:
        kind: question
        id: q_service_use
      operator: is_one_of
      value:
      - current
      - past6
      - pastold
    questions:
    - id: q_services
      type: checkbox
      required: true
      title: Which have you used? (select all that apply)
      options:
      - value: netflix
        label: Netflix
      - value: disney
        label: Disney+
      - value: max
        label: Max
      - value: prime
        label: Prime Video
      - value: other
        label: Another service
  - id: p_behavior
    display_logic:
      ref:
        kind: enrichment
        id: stream_search_count_30d
      operator: greater_than
      value: 0
    questions:
    - id: q_why_search
      type: textarea
      required: true
      probe: true
      title: We noticed you recently searched for "{{behavior:recent_stream_search}}". What were you hoping
        to find or decide?
  - id: p_detail
    display_logic:
      ref:
        kind: question
        id: q_service_use
      operator: equals
      value: current
    questions:
    - id: q_best_service
      type: radio
      title: Of the ones you currently use, which is your favourite?
      options_source:
        pipe_from: q_services
        mode: selected
        carry_values: true
    - id: q_attention
      type: radio
      required: true
      title: For quality control, please select 'Strongly agree'.
      options:
      - value: sd
        label: Strongly disagree
      - value: d
        label: Disagree
      - value: n
        label: Neutral
      - value: a
        label: Agree
      - value: sa
        label: Strongly agree
  actions:
  - type: set_variable
    name: segment
    value: streamer_current
    when:
      ref:
        kind: question
        id: q_service_use
      operator: equals
      value: current
  - type: set_variable
    name: engaged
    value: high
    when:
      ref:
        kind: enrichment
        id: ent_app_minutes_7d
      operator: greater_than
      value: 300
  - type: disqualify
    reason_code: no_consent
    dq_message: No problem — thanks for stopping by.
    when:
      ref:
        kind: question
        id: q_consent
      operator: equals
      value: 'no'
  - type: disqualify
    reason_code: under_age
    dq_message: Thanks! This study is for adults (18+).
    when:
      ref:
        kind: question
        id: q_age
      operator: less_than
      value: 18
  - type: disqualify
    reason_code: non_user
    dq_message: Thanks! You don't qualify for this particular study.
    when:
      ref:
        kind: question
        id: q_service_use
      operator: equals
      value: never
  - type: disqualify
    reason_code: failed_attention
    dq_message: Thanks for your responses.
    when:
      ref:
        kind: question
        id: q_attention
      operator: not_equals
      value: sa
  - type: overquota
    message: This study is now full — thank you!
    when:
      ref:
        kind: quota
        id: quota_overall
      operator: is_full
  - type: overquota
    message: This study is now full — thank you!
    when:
      op: and
      conditions:
      - ref:
          kind: question
          id: q_service_use
        operator: equals
        value: current
      - ref:
          kind: quota
          id: quota_current
        operator: is_full
  - type: complete
    redirect:
      kind: url
      url: https://example.com/thanks
---

# Streaming Services Usage

version 1

> Researcher-readable questionnaire with logic notes. The YAML frontmatter above is the **canonical source** — `survey-agent compile` regenerates the JSON from it. Edit the frontmatter (or the `.survey.json`), then re-render.

## Page `p_intro`

### 1. Please watch this short clip before we begin.  `video`

<sub>id `q_intro_video`</sub>

### 2. Welcome {{url:group|there}}! This short study is about video streaming services. Do you consent to take part?  `radio`  *(required)*

<sub>id `q_consent`</sub>

- `yes` — Yes, I consent

- `no` — No

_Piped: `{{url:group|there}}`._

## Page `p_screen`

### 3. What is your age?  `number`  *(required)*

<sub>id `q_age`</sub>

_Validation: range 16–99._

### 4. Have you used a paid video streaming service (e.g. Netflix, Disney+, Max)?  `radio`  *(required)*

<sub>id `q_service_use`</sub>

- `current` — Currently subscribe

- `past6` — Subscribed in the last 6 months

- `pastold` — Subscribed more than 6 months ago

- `never` — Never subscribed

_Options shuffled; fixed: never._

## Page `p_brands`

➡️ _Page shown only when Q `q_service_use` is one of current / past6 / pastold._

### 5. Which have you used? (select all that apply)  `checkbox`  *(required)*

<sub>id `q_services`</sub>

- `netflix` — Netflix

- `disney` — Disney+

- `max` — Max

- `prime` — Prime Video

- `other` — Another service

## Page `p_behavior`

➡️ _Page shown only when context `stream_search_count_30d` > 0._

### 6. We noticed you recently searched for "{{behavior:recent_stream_search}}". What were you hoping to find or decide?  `textarea`  *(required)*

<sub>id `q_why_search`</sub>

_Piped: `{{behavior:recent_stream_search}}`._

## Page `p_detail`

➡️ _Page shown only when Q `q_service_use` is current._

### 7. Of the ones you currently use, which is your favourite?  `radio`

<sub>id `q_best_service`</sub>

- _options piped from Q `q_services` (selected)_

### 8. For quality control, please select 'Strongly agree'.  `radio`  *(required)*

<sub>id `q_attention`</sub>

- `sd` — Strongly disagree

- `d` — Disagree

- `n` — Neutral

- `a` — Agree

- `sa` — Strongly agree

## Hidden values (computed, never shown to the respondent)

- `stream_score` = Q `q_service_use`

- `user_segment` = bucket(…)

## Quotas

- `quota_overall` — cap **500** (survey)

- `quota_current` — cap **300** (answer on Q `q_service_use` where answer in ['current'])

## Logic & terminals

- 🧮 **Set** `segment` — when Q `q_service_use` is current

- 🧮 **Set** `engaged` — when context `ent_app_minutes_7d` > 300

- ⛔ **Screen out** (reason `no_consent`) — when Q `q_consent` is no · “No problem — thanks for stopping by.”

- ⛔ **Screen out** (reason `under_age`) — when Q `q_age` < 18 · “Thanks! This study is for adults (18+).”

- ⛔ **Screen out** (reason `non_user`) — when Q `q_service_use` is never · “Thanks! You don't qualify for this particular study.”

- ⛔ **Screen out** (reason `failed_attention`) — when Q `q_attention` is not sa · “Thanks for your responses.”

- 🪣 **Over-quota** terminal — when quota `quota_overall` is full

- 🪣 **Over-quota** terminal — when (Q `q_service_use` is current AND quota `quota_current` is full)

- ✅ **Complete** → https://example.com/thanks

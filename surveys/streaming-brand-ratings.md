---
survey:
  id: streaming-brand-ratings
  version: 1
  title: Streaming Services — Ratings Study
  description: 'Worked example of question + page repeating: rate each service the respondent pays for,
    follow up only on the ones they love, ask about cancellation risk per service, and repeat a page once
    per household member.'
  tags:
  - example
  - repeating
  settings:
    url_variables:
    - uid
    - rid
    - group
    theme: paper
    default_redirect: default
  pages:
  - id: p_services
    questions:
    - id: q_services
      type: checkbox
      required: true
      title: Which streaming services do you currently pay for? Select all that apply.
      options:
      - value: netflix
        label: Netflix
      - value: prime
        label: Prime Video
      - value: disney
        label: Disney+
      - value: max
        label: Max
      - value: hulu
        label: Hulu
      - value: apple
        label: Apple TV+
      - value: none
        label: None of these
        exclusive: true
  - id: p_rate
    title: '{{loop:label}} ({{loop:index}} of {{loop:total}})'
    repeat:
      source: q_services
    questions:
    - id: q_sat
      type: rating
      required: true
      title: How satisfied are you with **{{loop:label}}**?
      scale_points:
      - 1
      - 2
      - 3
      - 4
      - 5
    - id: q_worth
      type: radio
      required: true
      title: Is {{loop:label}} worth what you pay for it?
      options:
      - value: 'yes'
        label: 'Yes'
      - value: 'no'
        label: 'No'
      - value: unsure
        label: Not sure
  - id: p_love
    title: You rated {{loop:label}} highly
    repeat:
      source: q_services
    questions:
    - id: q_love
      type: textarea
      title: What do you like most about {{loop:label}}?
      display_logic:
        ref:
          kind: question
          id: q_sat[@]
        operator: greater_or_equal
        value: 4
  - id: p_cancel
    title: Looking ahead
    questions:
    - id: q_cancel
      type: radio
      required: true
      title: How likely are you to cancel {{loop:label}} in the next 3 months?
      repeat:
        source: q_services
      options:
      - value: likely
        label: Likely
      - value: maybe
        label: Maybe
      - value: unlikely
        label: Unlikely
    - id: q_cancel_why
      type: text
      title: What would make you keep {{loop:label}}?
      repeat:
        source: q_services
      display_logic:
        ref:
          kind: question
          id: q_worth[@]
        operator: equals
        value: 'no'
  - id: p_household
    questions:
    - id: q_people
      type: number
      required: true
      title: Besides you, how many people in your household watch these services?
      validation:
        min: 0
        max: 6
  - id: p_person
    title: Household member {{loop:index}} of {{loop:total}}
    repeat:
      source: q_people
      by: value
      max: 6
    questions:
    - id: q_person_age
      type: radio
      required: true
      title: 'Person {{loop:index}}: which age group?'
      options:
      - value: u13
        label: Under 13
      - value: '13_17'
        label: 13–17
      - value: '18_34'
        label: 18–34
      - value: 35_plus
        label: 35+
  - id: p_end
    questions:
    - id: q_netflix_note
      type: text
      title: You rated Netflix {{q:q_sat[netflix].answer}} out of 5 — anything to add?
      display_logic:
        ref:
          kind: question
          id: q_sat[netflix]
        operator: is_answered
  actions:
  - type: disqualify
    reason_code: no_services
    dq_message: Thanks! This study is for people who pay for at least one streaming service.
    when:
      ref:
        kind: question
        id: q_services
      operator: contains
      value: none
  - type: complete
    redirect:
      kind: default
---

# Streaming Services — Ratings Study

_Worked example of question + page repeating: rate each service the respondent pays for, follow up only on the ones they love, ask about cancellation risk per service, and repeat a page once per household member._

tags: example, repeating · version 1

> Researcher-readable questionnaire with logic notes. The YAML frontmatter above is the **canonical source** — `survey-agent compile` regenerates the JSON from it. Edit the frontmatter (or the `.survey.json`), then re-render.

## Page `p_services`

### 1. Which streaming services do you currently pay for? Select all that apply.  `checkbox`  *(required)*

<sub>id `q_services`</sub>

- `netflix` — Netflix

- `prime` — Prime Video

- `disney` — Disney+

- `max` — Max

- `hulu` — Hulu

- `apple` — Apple TV+

- `none` — None of these  _(exclusive)_

## Page `p_rate` — {{loop:label}} ({{loop:index}} of {{loop:total}})

🔁 _Page repeats once per option chosen in Q `q_services`; its answers are stored per item as `qid[item]`._

### 2. How satisfied are you with **{{loop:label}}**?  `rating`  *(required)*

<sub>id `q_sat`</sub>

_Piped: `{{loop:label}}`._

### 3. Is {{loop:label}} worth what you pay for it?  `radio`  *(required)*

<sub>id `q_worth`</sub>

- `yes` — Yes

- `no` — No

- `unsure` — Not sure

_Piped: `{{loop:label}}`._

## Page `p_love` — You rated {{loop:label}} highly

🔁 _Page repeats once per option chosen in Q `q_services`; its answers are stored per item as `qid[item]`._

### 4. What do you like most about {{loop:label}}?  `textarea`

<sub>id `q_love`</sub>

➡️ _Shown only when this item's answer to Q `q_sat` ≥ 4._

_Piped: `{{loop:label}}`._

## Page `p_cancel` — Looking ahead

🔁 _Asked once per option chosen in Q `q_services` — answers stored as `q_cancel[item]`._

### 5. How likely are you to cancel {{loop:label}} in the next 3 months?  `radio`  *(required)*

<sub>id `q_cancel`</sub>

- `likely` — Likely

- `maybe` — Maybe

- `unlikely` — Unlikely

_Piped: `{{loop:label}}`._

🔁 _Asked once per option chosen in Q `q_services` — answers stored as `q_cancel_why[item]`._

### 6. What would make you keep {{loop:label}}?  `text`

<sub>id `q_cancel_why`</sub>

➡️ _Shown only when this item's answer to Q `q_worth` is no._

_Piped: `{{loop:label}}`._

## Page `p_household`

### 7. Besides you, how many people in your household watch these services?  `number`  *(required)*

<sub>id `q_people`</sub>

_Validation: range 0–6._

## Page `p_person` — Household member {{loop:index}} of {{loop:total}}

🔁 _Page repeats once per count in Q `q_people` (1…N) (max 6); its answers are stored per item as `qid[item]`._

### 8. Person {{loop:index}}: which age group?  `radio`  *(required)*

<sub>id `q_person_age`</sub>

- `u13` — Under 13

- `13_17` — 13–17

- `18_34` — 18–34

- `35_plus` — 35+

_Piped: `{{loop:index}}`._

## Page `p_end`

### 9. You rated Netflix {{q:q_sat[netflix].answer}} out of 5 — anything to add?  `text`

<sub>id `q_netflix_note`</sub>

➡️ _Shown only when Q `q_sat[netflix]` is answered._

_Piped: `{{q:q_sat[netflix].answer}}`._

## Logic & terminals

- ⛔ **Screen out** (reason `no_services`) — when Q `q_services` contains none · “Thanks! This study is for people who pay for at least one streaming service.”

- ✅ **Complete** → default

# Database entity-relationship schema

This diagram reflects the current SQLAlchemy models in
`apps/static/assets/utils/database_classes.py`. Optional foreign keys are shown with
`o|`; required foreign keys are shown with `||`. Every table also contains the required
`created_at` and `updated_at` fields supplied by `TimestampMixin`.

![PlanktonLab database entity-relationship diagram](ER_DIAGRAM.png)

The Mermaid source below provides a scalable, editable version of the same schema.

```mermaid
erDiagram
    user {
        int id PK
        string username UK
        string email
        datetime created_at
        datetime updated_at
    }

    lakes {
        int id PK
        string lake "unique with country"
        string country "unique with lake"
        float depth "nullable; metres"
        float area "nullable; square kilometres"
        float latitude "nullable"
        float longitude "nullable"
        float elevation_m "nullable"
        float mean_depth_m "nullable"
        float volume_km3 "nullable"
        float shoreline_length_km "nullable"
        float catchment_area_km2 "nullable"
        float residence_time_days "nullable"
        string waterbody_type "nullable"
        string administrative_region "nullable"
        string external_source "nullable"
        string external_id "nullable"
        text source_url "nullable"
        datetime metadata_retrieved_at "nullable"
        datetime created_at
        datetime updated_at
    }

    sampling_metadata {
        int id PK
        int user_id FK
        int lake_id FK
        string image_name UK
        date sampling_date
        string image_path
        datetime created_at
        datetime updated_at
    }

    taxa {
        int id PK
        string scientific_name UK
        string common_name "nullable"
        string rank
        int parent_id FK "nullable"
        datetime created_at
        datetime updated_at
    }

    model_versions {
        int id PK
        string name "unique with version"
        string version "unique with name"
        string artifact_path "nullable"
        text metrics_json "nullable"
        boolean is_active
        datetime created_at
        datetime updated_at
    }

    model_runs {
        int id PK
        int model_version_id FK "nullable"
        int sampling_metadata_id FK
        string status "running, completed, or failed"
        text parameters_json "nullable"
        text error_message "nullable"
        datetime started_at
        datetime completed_at "nullable"
        datetime created_at
        datetime updated_at
    }

    samples {
        int id PK
        int sampling_metadata_id FK
        int x
        int y
        int w
        int h
        string species
        float confidence
        text image_base64
        int taxon_id FK "nullable"
        int model_run_id FK "nullable"
        string review_status "unreviewed or reviewed"
        datetime created_at
        datetime updated_at
    }

    predictions {
        int id PK
        int sample_id FK
        int taxon_id FK
        int model_run_id FK "nullable"
        float confidence
        string source "model or import"
        datetime created_at
        datetime updated_at
    }

    reviews {
        int id PK
        int sample_id FK
        int reviewer_id FK "nullable"
        int taxon_id FK "nullable"
        string previous_species "nullable"
        string status "accepted, corrected, or rejected"
        text notes "nullable"
        datetime created_at
        datetime updated_at
    }

    user ||--o{ sampling_metadata : creates
    lakes ||--o{ sampling_metadata : identifies
    taxa o|--o{ taxa : parent_of
    model_versions o|--o{ model_runs : executes
    sampling_metadata ||--o{ model_runs : has
    sampling_metadata ||--o{ samples : contains
    taxa o|--o{ samples : current_taxon
    model_runs o|--o{ samples : current_run
    samples ||--o{ predictions : records
    taxa ||--o{ predictions : predicts
    model_runs o|--o{ predictions : produces
    samples ||--o{ reviews : receives
    user o|--o{ reviews : performs
    taxa o|--o{ reviews : assigns
```

## Delete behavior

| Parent | Child foreign key | Behavior |
| --- | --- | --- |
| `user` | `sampling_metadata.user_id` | Restrict deletion |
| `lakes` | `sampling_metadata.lake_id` | Restrict deletion |
| `taxa` | `taxa.parent_id` | Set child reference to null |
| `model_versions` | `model_runs.model_version_id` | Set reference to null |
| `sampling_metadata` | `model_runs.sampling_metadata_id` | Cascade deletion |
| `sampling_metadata` | `samples.sampling_metadata_id` | Cascade deletion |
| `taxa` | `samples.taxon_id` | Set reference to null |
| `model_runs` | `samples.model_run_id` | Set reference to null |
| `samples` | `predictions.sample_id` | Cascade deletion |
| `taxa` | `predictions.taxon_id` | Restrict deletion |
| `model_runs` | `predictions.model_run_id` | Set reference to null |
| `samples` | `reviews.sample_id` | Cascade deletion |
| `user` | `reviews.reviewer_id` | Set reference to null |
| `taxa` | `reviews.taxon_id` | Restrict deletion |

The database also creates three read-only reporting views:

- `classification_summary` aggregates detections by image and taxon.
- `review_queue` exposes unreviewed detections with their image and lake context.
- `model_run_summary` reports model provenance and prediction counts per run.

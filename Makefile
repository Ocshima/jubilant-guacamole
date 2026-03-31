# ============================================================
# Makefile — Trade ETL Pipeline
# ============================================================

STACK_NAME   ?= trade-etl-pipeline
REGION       ?= us-east-1
ENVIRONMENT  ?= dev

BUCKET    ?= $(shell aws cloudformation describe-stacks \
               --stack-name $(STACK_NAME) --region $(REGION) \
               --query "Stacks[0].Outputs[?OutputKey=='RawUploadsBucketName'].OutputValue" \
               --output text 2>/dev/null)
TABLE     ?= $(shell aws cloudformation describe-stacks \
               --stack-name $(STACK_NAME) --region $(REGION) \
               --query "Stacks[0].Outputs[?OutputKey=='TradesTableName'].OutputValue" \
               --output text 2>/dev/null)
DLQ_URL   ?= $(shell aws cloudformation describe-stacks \
               --stack-name $(STACK_NAME) --region $(REGION) \
               --query "Stacks[0].Outputs[?OutputKey=='DLQUrl'].OutputValue" \
               --output text 2>/dev/null)
FUNC_NAME ?= $(shell aws cloudformation describe-stacks \
               --stack-name $(STACK_NAME) --region $(REGION) \
               --query "Stacks[0].Outputs[?OutputKey=='LambdaFunctionName'].OutputValue" \
               --output text 2>/dev/null)

.PHONY: help deploy-bootstrap test build deploy upload-valid upload-invalid \
        upload-missing upload-malformed check-dlq drain-dlq \
        tail-logs outputs destroy

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ── Development ───────────────────────────────────────────────
test: ## Run unit tests with coverage
	cd src/processor && python -m pytest ../../tests/ -v \
	  --cov=. --cov-report=term-missing

test-validator: ## Run validator tests only
	cd src/processor && python -m pytest ../../tests/test_validator.py -v

test-transformer: ## Run transformer tests only
	cd src/processor && python -m pytest ../../tests/test_transformer.py -v

lint: ## Lint the CloudFormation template
	cfn-lint infrastructure/template.yaml --include-checks W

# ── Bootstrap (run once) ──────────────────────────────────────
deploy-bootstrap: ## Deploy the OIDC role for this project (run once; reuses Project 1's OIDC provider)
	@[ -n "$(GITHUB_ORG)" ]       || (echo "❌ Set GITHUB_ORG=your-username"         && exit 1)
	@[ -n "$(GITHUB_REPO)" ]      || (echo "❌ Set GITHUB_REPO=your-repo"            && exit 1)
	@[ -n "$(OIDC_PROVIDER_ARN)" ] || (echo "❌ Set OIDC_PROVIDER_ARN=arn:aws:iam::...:oidc-provider/token.actions.githubusercontent.com" && exit 1)
	aws cloudformation deploy \
	  --template-file infrastructure/oidc-bootstrap.yaml \
	  --stack-name github-oidc-bootstrap-etl \
	  --parameter-overrides \
	      GitHubOrg=$(GITHUB_ORG) \
	      GitHubRepo=$(GITHUB_REPO) \
	      OIDCProviderArn=$(OIDC_PROVIDER_ARN) \
	      MainStackName=$(STACK_NAME) \
	  --capabilities CAPABILITY_NAMED_IAM \
	  --region $(REGION)
	@echo "✅ Bootstrap deployed — copy RoleArn into GitHub secrets as AWS_ROLE_ARN"

# ── Build & Deploy ─────────────────────────────────────────────
build: ## SAM build (packages Lambda code)
	sam build --template infrastructure/template.yaml

deploy: build ## Build and deploy the stack
	@[ -n "$(ALERT_EMAIL)" ] || (echo "❌ Set ALERT_EMAIL=you@example.com" && exit 1)
	sam deploy \
	  --stack-name $(STACK_NAME) \
	  --parameter-overrides \
	      Environment=$(ENVIRONMENT) \
	      AlertEmail=$(ALERT_EMAIL) \
	  --capabilities CAPABILITY_NAMED_IAM \
	  --resolve-s3 \
	  --no-fail-on-empty-changeset \
	  --region $(REGION)
	@$(MAKE) outputs

# ── Mock Data Upload ──────────────────────────────────────────
upload-valid: ## Upload valid trades CSV -> triggers pipeline with clean data
	@[ -n "$(BUCKET)" ] || (echo "❌ Stack not deployed" && exit 1)
	aws s3 cp mock-data/valid/trades_valid.csv \
	  s3://$(BUCKET)/uploads/trades_valid_$$(date +%Y%m%d_%H%M%S).csv
	@echo "✅ Uploaded valid data — check CloudWatch logs in ~5 seconds"

upload-invalid-values: ## Upload invalid values CSV -> all rows should hit DLQ
	@[ -n "$(BUCKET)" ] || (echo "❌ Stack not deployed" && exit 1)
	aws s3 cp mock-data/invalid/trades_invalid_values.csv \
	  s3://$(BUCKET)/uploads/trades_invalid_values_$$(date +%Y%m%d_%H%M%S).csv
	@echo "✅ Uploaded invalid values — watch DLQ depth alarm"

upload-missing: ## Upload missing fields CSV -> all rows should hit DLQ
	@[ -n "$(BUCKET)" ] || (echo "❌ Stack not deployed" && exit 1)
	aws s3 cp mock-data/invalid/trades_missing_fields.csv \
	  s3://$(BUCKET)/uploads/trades_missing_$$(date +%Y%m%d_%H%M%S).csv

upload-malformed: ## Upload malformed CSV -> mixed valid/invalid rows
	@[ -n "$(BUCKET)" ] || (echo "❌ Stack not deployed" && exit 1)
	aws s3 cp mock-data/invalid/trades_malformed.csv \
	  s3://$(BUCKET)/uploads/trades_malformed_$$(date +%Y%m%d_%H%M%S).csv

upload-all-invalid: ## Upload all three invalid files in sequence
	@$(MAKE) upload-invalid-values
	@$(MAKE) upload-missing
	@$(MAKE) upload-malformed
	@echo "✅ All invalid files uploaded"

# ── Observability ─────────────────────────────────────────────
tail-logs: ## Tail Lambda logs in real time
	@[ -n "$(FUNC_NAME)" ] || (echo "❌ Stack not deployed" && exit 1)
	aws logs tail /aws/lambda/$(FUNC_NAME) --follow --format short

check-dlq: ## Show current DLQ message count and sample a message
	@[ -n "$(DLQ_URL)" ] || (echo "❌ Stack not deployed" && exit 1)
	@DEPTH=$$(aws sqs get-queue-attributes \
	  --queue-url "$(DLQ_URL)" \
	  --attribute-names ApproximateNumberOfMessages \
	  --query 'Attributes.ApproximateNumberOfMessages' \
	  --output text); \
	echo "DLQ depth: $$DEPTH messages"; \
	if [ "$$DEPTH" != "0" ]; then \
	  echo "--- Sampling first message ---"; \
	  aws sqs receive-message \
	    --queue-url "$(DLQ_URL)" \
	    --max-number-of-messages 1 \
	    --query 'Messages[0].Body' \
	    --output text | python3 -m json.tool; \
	fi

drain-dlq: ## ⚠️  Delete all messages from the DLQ
	@echo "⚠️  This will permanently delete all DLQ messages"
	@read -p "   Type 'drain' to confirm: " confirm; \
	  [ "$$confirm" = "drain" ] || (echo "Aborted." && exit 1)
	aws sqs purge-queue --queue-url "$(DLQ_URL)"
	@echo "✅ DLQ drained"

outputs: ## Print stack outputs
	aws cloudformation describe-stacks \
	  --stack-name $(STACK_NAME) \
	  --region $(REGION) \
	  --query "Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}" \
	  --output table

# ── Teardown ──────────────────────────────────────────────────
destroy: ## ⚠️  Delete the stack (TradesTable is RETAINED)
	@echo "⚠️  Deleting stack: $(STACK_NAME)"
	@echo "   NOTE: TradesTable has DeletionPolicy: Retain — data is preserved"
	@read -p "   Type the stack name to confirm: " confirm; \
	  [ "$$confirm" = "$(STACK_NAME)" ] || (echo "Aborted." && exit 1)
	@echo "🗑️  Emptying S3 bucket..."
	-aws s3 rm s3://$(BUCKET) --recursive --region $(REGION)
	@echo "🗑️  Draining DLQ..."
	-aws sqs purge-queue --queue-url "$(DLQ_URL)" 2>/dev/null || true
	aws cloudformation delete-stack \
	  --stack-name $(STACK_NAME) \
	  --region $(REGION)
	aws cloudformation wait stack-delete-complete \
	  --stack-name $(STACK_NAME) \
	  --region $(REGION)
	@echo "✅ Stack deleted. TradesTable retained — delete manually if needed."

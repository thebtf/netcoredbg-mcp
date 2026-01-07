# Testing Guidelines

## Core Principles

1. **Tests are NOT optional** — All new code requires tests
2. **Regression test FIRST** — For bug fixes, write failing test before fixing
3. **Test behavior, not implementation** — Tests should survive refactoring
4. **One assertion per test** — Keep tests focused

## Coverage Targets

| Category | Target | Notes |
|----------|--------|-------|
| Core business logic | 80% | Critical functionality |
| Critical paths | 100% | File I/O, data persistence, core workflows |
| Utilities/helpers | 70% | Supporting code |
| UI components | 60% | Where practical |

## Test Naming Convention

```
Feature_Scenario_ExpectedResult
```

Examples:
- `Calculator_AddTwoNumbers_ReturnsSum`
- `UserService_InvalidEmail_ThrowsValidationException`
- `FileParser_EmptyFile_ReturnsEmptyList`

## Test Structure (AAA Pattern)

```
// Arrange
[Setup test data and dependencies]

// Act
[Execute the code under test]

// Assert
[Verify expected outcome]
```

## Test Categories

Use traits/categories to organize tests:

| Category | Description | When to Run |
|----------|-------------|-------------|
| Unit | Isolated, fast, no I/O | Always |
| Integration | Multiple components | CI/CD |
| E2E | Full system | Pre-release |
| Smoke | Critical paths only | Quick validation |

## Bug Fix Workflow

**NON-NEGOTIABLE: Regression test FIRST**

1. ❌ Write test that reproduces the bug (should FAIL)
2. ✅ Fix the bug
3. ✅ Verify test now passes
4. ✅ Run full test suite

## What to Test

### DO Test
- Public API contracts
- Edge cases and boundaries
- Error conditions
- State transitions
- Business rules

### DON'T Test
- Private implementation details
- Framework/library code
- Trivial getters/setters
- Third-party integrations (mock them)

## Mocking Guidelines

- Mock external dependencies (APIs, databases, file system)
- Don't mock the code under test
- Prefer fakes over mocks when possible
- Verify mock interactions sparingly

## Test Data

- Use descriptive variable names
- Create test data factories/builders for complex objects
- Avoid magic numbers — use constants
- Keep test data minimal but complete

## Common Anti-Patterns

| Anti-Pattern | Problem | Solution |
|--------------|---------|----------|
| Testing implementation | Brittle tests | Test behavior |
| Multiple assertions | Hard to debug | One assertion per test |
| Shared mutable state | Flaky tests | Isolate test data |
| Testing private methods | Coupling | Test through public API |
| No assertion | Silent failure | Always assert |

## Framework-Specific Notes

[Customize this section for your testing framework]

### Example: xUnit (.NET)
```csharp
[Fact]
public void Method_Scenario_Expected() { }

[Theory]
[InlineData(1, 2, 3)]
public void Method_WithData_Expected(int a, int b, int expected) { }

[Trait("Category", "Integration")]
public void IntegrationTest() { }
```

### Example: pytest (Python)
```python
def test_method_scenario_expected():
    pass

@pytest.mark.parametrize("a,b,expected", [(1, 2, 3)])
def test_method_with_data(a, b, expected):
    pass

@pytest.mark.integration
def test_integration():
    pass
```

### Example: Jest (JavaScript)
```javascript
describe('Feature', () => {
  it('should do expected thing when scenario', () => {
    // test
  });
});
```

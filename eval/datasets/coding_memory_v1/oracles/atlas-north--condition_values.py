"""Immutable oracle for atlas-north:condition_values."""
import service


def main():
    assert service.retry_budget() == {'limit': 50, 'unit': 'requests/minute', 'enabled': True}, service.retry_budget()


if __name__ == "__main__":
    main()

"""Immutable oracle for atlas-violet:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-atlas-violet', service.current_policy()


if __name__ == "__main__":
    main()

"""Immutable oracle for atlas-north:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-atlas-north', service.current_policy()


if __name__ == "__main__":
    main()

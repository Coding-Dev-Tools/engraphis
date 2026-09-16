"""Immutable oracle for ember-north:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-ember-north', service.current_policy()


if __name__ == "__main__":
    main()

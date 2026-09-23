"""Immutable oracle for juniper-north:temporal_history."""
import service


def main():
    assert service.current_policy() == 'strict-juniper-north', service.current_policy()


if __name__ == "__main__":
    main()

"""Immutable oracle for helios-violet:scope_boundaries."""
import service


def main():
    assert service.scope_owner() == 'repo-helios-violet', service.scope_owner()


if __name__ == "__main__":
    main()

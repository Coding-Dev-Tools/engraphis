"""Immutable oracle for helios-violet:long_documents."""
import service


def main():
    assert service.find_section('late-constraint') == 'The catalog path retains 45 days and uses America/New_York.', service.find_section('late-constraint')


if __name__ == "__main__":
    main()

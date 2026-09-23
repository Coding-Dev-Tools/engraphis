"""Immutable oracle for helios-green:paraphrases."""
import service


def main():
    assert service.lookup_paraphrase('current policy') == 'strict-helios-green', service.lookup_paraphrase('current policy')


if __name__ == "__main__":
    main()
